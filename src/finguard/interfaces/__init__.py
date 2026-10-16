"""User-facing adapters for the FinGuard ApplicationService.

The interface package deliberately contains no agent, persistence, or account
logic.  Both the command line and HTTP adapters call the same async service.
"""

from .protocol import ApplicationServiceProtocol
from .service_loader import ServiceUnavailable

__all__ = ["ApplicationServiceProtocol", "ServiceUnavailable"]
