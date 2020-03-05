#!/usr/bin/env python
# -*- encoding: utf-8 -*-

# Copyright 2011-2020, Nigel Small
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from collections import OrderedDict
from logging import getLogger
from json import dumps as json_dumps, loads as json_loads

from certifi import where
from urllib3 import HTTPConnectionPool, HTTPSConnectionPool, make_headers

from py2neo import http_user_agent
from py2neo.internal.compat import urlsplit
from py2neo.internal.hydration import JSONHydrator
from py2neo.net import Connection
from py2neo.net.api import Result, Transaction


log = getLogger(__name__)


class HTTP(Connection):

    scheme = "http"

    protocol_version = (1, 1)

    @classmethod
    def open(cls, profile, user_agent=None):
        http = cls(profile, (user_agent or http_user_agent()))
        http.hello()
        return http

    def __init__(self, profile, user_agent):
        super(HTTP, self).__init__(profile, user_agent)
        self.http_pool = None
        self.headers = make_headers(basic_auth=":".join(profile.auth))
        self.transactions = {}
        self.__closed = False
        self._make_pool(profile)
        self._supports_multi = None

    def _make_pool(self, profile):
        self.http_pool = HTTPConnectionPool(
            host=profile.host,
            port=profile.port_number,
            maxsize=1,
            block=True,
        )

    def close(self):
        self.http_pool.close()
        self.__closed = True

    @property
    def closed(self):
        return self.__closed

    @property
    def broken(self):
        return False

    @property
    def local_port(self):
        raise NotImplementedError

    def hello(self):
        r = self.http_pool.request(method="GET",
                                   url="/",
                                   headers=dict(self.headers))
        metadata = json_loads(r.data.decode("utf-8"))
        if "neo4j_version" in metadata:     # Neo4j 4.x
            # {
            #   "bolt_routing" : "neo4j://localhost:7687",
            #   "transaction" : "http://localhost:7474/db/{databaseName}/tx",
            #   "bolt_direct" : "bolt://localhost:7687",
            #   "neo4j_version" : "4.0.0",
            #   "neo4j_edition" : "community"
            # }
            neo4j_version = metadata["neo4j_version"]
            self._supports_multi = True
        else:                               # Neo4j 3.x
            # {
            #   "data" : "http://localhost:7474/db/data/",
            #   "management" : "http://localhost:7474/db/manage/",
            #   "bolt" : "bolt://localhost:7687"
            # }
            r = self.http_pool.request(method="GET",
                                       url="/db/data/",
                                       headers=dict(self.headers))
            metadata = json_loads(r.data.decode("utf-8"))
            # {
            #   "extensions" : { },
            #   "node" : "http://localhost:7474/db/data/node",
            #   "relationship" : "http://localhost:7474/db/data/relationship",
            #   "node_index" : "http://localhost:7474/db/data/index/node",
            #   "relationship_index" : "http://localhost:7474/db/data/index/relationship",
            #   "extensions_info" : "http://localhost:7474/db/data/ext",
            #   "relationship_types" : "http://localhost:7474/db/data/relationship/types",
            #   "batch" : "http://localhost:7474/db/data/batch",
            #   "cypher" : "http://localhost:7474/db/data/cypher",
            #   "indexes" : "http://localhost:7474/db/data/schema/index",
            #   "constraints" : "http://localhost:7474/db/data/schema/constraint",
            #   "transaction" : "http://localhost:7474/db/data/transaction",
            #   "node_labels" : "http://localhost:7474/db/data/labels",
            #   "neo4j_version" : "3.5.12"
            # }
            neo4j_version = metadata["neo4j_version"]
            self._supports_multi = False
        self.server_agent = "Neo4j/{}".format(neo4j_version)

    def _transaction_uri(self, db, *segments):
        if db:
            if self._supports_multi:
                return "/".join(("db", db, "tx") + tuple(map(str, segments)))
            else:
                # TODO: FeatureError or similar
                raise RuntimeError("Multiple databases are not supported "
                                   "with this version of Neo4j")
        else:
            return "/".join(("db", "data", "transaction") + tuple(map(str, segments)))

    def auto_run(self, cypher, parameters=None,
                 db=None, readonly=False, bookmarks=None, metadata=None, timeout=None):
        r = self._post(self._transaction_uri(db, "commit"), cypher, parameters)
        assert r.status == 200  # TODO: other codes
        data = json_loads(r.data.decode("utf-8"), object_hook=JSONHydrator.json_to_packstream)
        result_data = data["results"][0] if data["results"] else {}
        return HTTPResult(result_data, data["errors"])

    def begin(self, db=None, readonly=False, bookmarks=None, metadata=None, timeout=None):
        r = self._post(self._transaction_uri(db))
        if r.status == 201:
            location_path = urlsplit(r.headers["Location"]).path
            tx = location_path.rpartition("/")[-1]
            self.transactions[tx] = Transaction(db=db)
            return tx
        else:
            raise RuntimeError("Can't begin a new transaction")

    def commit(self, tx):
        self._assert_valid_tx(tx)
        transaction = self.transactions.pop(tx)
        self._post(self._transaction_uri(transaction.db, tx, "commit"))

    def rollback(self, tx):
        self._assert_valid_tx(tx)
        transaction = self.transactions.pop(tx)
        self._delete(self._transaction_uri(transaction.db, tx))

    def run_in_tx(self, tx, cypher, parameters=None):
        transaction = self.transactions[tx]
        r = self._post(self._transaction_uri(transaction.db, tx), cypher, parameters)
        assert r.status == 200  # TODO: other codes
        data = json_loads(r.data.decode("utf-8"), object_hook=JSONHydrator.json_to_packstream)
        result_data = data["results"][0] if data["results"] else {}
        return HTTPResult(result_data, data.get("errors"), profile=self.profile)

    def pull(self, result, n=-1):
        pass

    def discard(self, result, n=-1):
        pass

    def sync(self, result):
        self._audit(result)

    def _audit(self, result):
        result.audit()

    def fetch(self, result):
        record = result.take_record()
        return record

    def _assert_valid_tx(self, tx):
        from py2neo.database import TransactionError
        if not tx:
            raise TransactionError("No transaction")
        if tx not in self.transactions:
            raise TransactionError("Invalid transaction")

    def _post(self, url, statement=None, parameters=None):
        if statement:
            statements = [
                OrderedDict([
                    ("statement", statement),
                    ("parameters", parameters or {}),
                    ("resultDataContents", ["REST"]),
                    ("includeStats", True),
                ])
            ]
        else:
            statements = []
        return self.http_pool.request(method="POST",
                                      url=url,
                                      headers=dict(self.headers, **{"Content-Type": "application/json"}),
                                      body=json_dumps({"statements": statements}))

    def _delete(self, url):
        return self.http_pool.request(method="DELETE",
                                      url=url,
                                      headers=dict(self.headers))


class HTTPS(HTTP):

    scheme = "https"

    def _make_pool(self, service):
        cert_reqs = None  # TODO: expose this through service
        self.http_pool = HTTPSConnectionPool(
            host=service.host,
            port=service.port_number,
            maxsize=1,
            block=True,
            cert_reqs=(cert_reqs or "CERT_NONE"),
            ca_certs=where()
        )


class HTTPResult(Result):

    def __init__(self, result, errors, profile=None):
        super(Result, self).__init__()
        self._columns = result.get("columns", ())
        self._data = result.get("data", [])
        self._summary = {}
        if "stats" in result:
            self._summary["stats"] = result["stats"]
        if profile:
            self._summary["connection"] = profile.to_dict()
        self._errors = errors
        self._cursor = 0

    def audit(self):
        if self._errors:
            from py2neo.database import GraphError
            failure = GraphError.hydrate(self._errors.pop(0))
            raise failure

    def buffer(self):
        pass

    def fields(self):
        return self._columns

    def summary(self):
        return self._summary

    def fetch(self):
        return self.take_record()

    def has_records(self):
        return self._cursor < len(self._data)

    def take_record(self):
        try:
            record = self._data[self._cursor]["rest"]
        except IndexError:
            return None
        else:
            self._cursor += 1
            return record