"""Information workspace for monthly bills, allowances and subscribed services."""
from .bills import register_bills
from .services import register_services
from .usage import register_usage


def register_information(app, accounts, credentials, *, recover=None, accept_credentials=None):
    for register in (register_bills, register_usage, register_services):
        register(app, accounts, credentials, recover=recover, accept_credentials=accept_credentials)
