"""Model calls: the prompts, the response schemas and the gateway that sends them.

* ``prompts``      the user-editable instructions and the ``PromptStore``
* ``schemas``      what each call must answer with
* ``tagging``, ``bullets``, ``outreach``, ``corrections``
                   the message each operation sends, as pure functions
* ``gateway``      ``ModelGateway``: every call, its bookkeeping and checks
* ``backends``     how a call reaches a model, and which way is live

Only the gateway talks to a backend. Whether a call goes to the Anthropic API
or a local CLI is the backends' problem, never the prompts'.
"""
from .backends import BackendRegistry, default_registry
from .gateway import BackendUnavailable, ModelGateway
from .prompts import EDITABLE_PROMPTS, PromptStore

__all__ = ["BackendRegistry", "BackendUnavailable", "EDITABLE_PROMPTS", "ModelGateway",
           "PromptStore", "default_registry"]
