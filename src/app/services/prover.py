#
# Copyright 2017-2018 Government of Canada
# Public Services and Procurement Canada - buyandsell.gc.ca
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import os
import logging
from random import randint

from app.services import eventloop
from app.services.exchange import Exchange, ExchangeError, RequestExecutor
from app.services.tob import TobClient, TobClientError
from app.services.von import VonClient
from app.settings import expand_tree_variables
from app.util import log_json

LOGGER = logging.getLogger(__name__)


def init_prover_manager(config, env=None, exchange=None, pid='prover-manager'):
    if not config:
        raise ValueError('Missing configuration for prover manager')
    if not env:
        env = os.environ
    if not exchange:
        LOGGER.info('Starting new Exchange service for issuer manager')
        exchange = Exchange()
        exchange.start()
    replace_vars = os.environ.copy()
    replace_vars.update(env)
    config_requests = expand_tree_variables(config['proof_requests'], replace_vars)
    LOGGER.info('Initializing proof request manager')
    return ProverManager(pid, exchange, env, config_requests)


class ProverError(ExchangeError):
    pass


class ConstructProofRequest:
    def __init__(self, name, filters):
        self.name = name
        self.filters = filters


class ConstructProofResponse:
    def __init__(self, value):
        self.value = value


class ProverManager(RequestExecutor):
    """
    There should only be one instance of this class in the application.
    It is responsible for packaging proof requests, sending them to TheOrgBook
    and returning the results.
    """

    def __init__(self, pid, exchange, env, request_specs):
        super(ProverManager, self).__init__(pid, exchange)
        self._env = env or {}
        self._orgbook_did = None
        self._request_specs = request_specs or {}
        self._ready = True

    def ready(self):
        return self._ready

    def status(self):
        return {
            'orgbook_did': self._orgbook_did,
            'ready': self._ready
        }

    @property
    def request_specs(self):
        return self._request_specs

    def init_von_client(self):
        cfg = {
            'genesis_path': self._env.get('INDY_GENESIS_PATH'),
            'ledger_url': self._env.get('INDY_LEDGER_URL'),
            'wallet_name': 'Generic', # FIXME
            'wallet_seed': 'verifier-seed-000000000000000000' # FIXME - what seed to use here?
        }
        return VonClient(cfg)

    def init_tob_client(self):
        cfg = {
            'api_url': self._env.get('TOB_API_URL')
        }
        return TobClient(cfg)

    def _prepare_request_json(self, name):
        spec = self._request_specs.get(name)
        if not spec:
            raise ValueError('Proof request not defined: {}'.format(name))
        request_json = {
            'name': spec.get('name', name),
            'nonce': str(randint(10000000000, 100000000000)),  # FIXME - how best to generate?
            'version': spec['version']
        }
        req_attrs = {}
        for schema in spec['schemas']:
            for attr in schema['attributes']:
                # FIXME - support attribute renaming
                req_attrs[attr] = {
                    'name': attr,
                    'restrictions': [{
                        # schema_key can include name, version, and did
                        'schema_key': schema['key'].copy()
                    }]
                }
        request_json['requested_attrs'] = req_attrs
        request_json['requested_predicates'] = {}
        return request_json

    async def construct_proof(self, name, filters):
        proof_request = self._prepare_request_json(name)

        tob_client = self.init_tob_client()
        von_client = self.init_von_client()

        log_json('Requesting proof:', {
            'filters': filters,
            'proof_request': proof_request
        }, LOGGER)

        try:
            proof_response = tob_client.create_record('bcovrin/construct-proof', {
                'filters': filters,
                'proof_request': proof_request
            })
            log_json('Got proof response:', proof_response, LOGGER)
        except TobClientError as e:
            if e.status_code == 406:
                return {'success': False, 'error': e.response.json()['detail']}
            LOGGER.exception('Error response while requesting proof:')
            return {'success': False, 'error': 'Unexpected response from server'}

        proof = proof_response['proof']
        parsed_proof = {}
        for attr in proof['requested_proof']['revealed_attrs']:
            parsed_proof[attr] = \
                proof['requested_proof']['revealed_attrs'][attr][1]

        async with von_client.create_verifier() as von_verifier:
            verified = await von_verifier.verify_proof(
                proof_request,
                proof
            )

        return {
            'success': True,
            'value': {
                'proof': proof,
                'parsed_proof': parsed_proof,
                'verified': verified
            }
        }

    async def _process_construct_proof(self, from_pid, ident, message):
        try:
            result = await self.construct_proof(message.name, message.filters)
            if result['success']:
                reply = ConstructProofResponse(result['value'])
            else:
                reply = ProverError(result['error'])
            self.send_noreply(from_pid, reply, ident)
        except Exception:
            LOGGER.exception('Exception while constructing proof request:')
            msg = ProverError('Exception while constructing proof request')
            self.send_noreply(from_pid, msg, ident)

    def process(self, from_pid, ident, message, ref):
        if isinstance(message, ConstructProofRequest):
            spec = self._request_specs.get(message.name)
            if not spec:
                self.send_noreply(from_pid, ProverError('Proof request not defined'), ident)
            else:
                coro = self._process_construct_proof(from_pid, ident, message)
                eventloop.run_in_executor(self._pool, coro)
        elif message == 'ready':
            self.send_noreply(from_pid, self.ready(), ident)
        elif message == 'status':
            self.send_noreply(from_pid, self.status(), ident)
        else:
            raise ValueError('Unexpected message from {}: {}'.format(from_pid, message))
