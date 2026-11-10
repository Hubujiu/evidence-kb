from copy import deepcopy
from typing import ClassVar

from pydantic import RootModel, model_validator

from .knowledge_contracts import SESSION_RESPONSE, TURN_RESPONSE, PUBLIC_REQUEST_STATUS_RESPONSE
from .plugin_contracts import schema_validator
from .document_contracts import DOCUMENTS_RESPONSE, VERSIONS_RESPONSE


class ContractResponse(RootModel[dict]):
    contract_schema: ClassVar[dict]

    @model_validator(mode='after')
    def validate_contract(self):
        error = next(schema_validator(self.contract_schema).iter_errors(self.root), None)
        if error is not None:
            raise ValueError(error.message)
        return self

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        return deepcopy(cls.contract_schema)


class SessionResponse(ContractResponse):
    contract_schema = SESSION_RESPONSE


class TurnResponse(ContractResponse):
    contract_schema = TURN_RESPONSE


class RequestStatusResponse(ContractResponse):
    contract_schema = PUBLIC_REQUEST_STATUS_RESPONSE


class DocumentsResponse(ContractResponse):
    contract_schema = DOCUMENTS_RESPONSE


class SourceVersionsResponse(ContractResponse):
    contract_schema = VERSIONS_RESPONSE
