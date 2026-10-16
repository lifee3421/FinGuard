"""Resolve the core ApplicationService without importing it at package load."""

from __future__ import annotations

import importlib
import os
from typing import Any


class ServiceUnavailable(RuntimeError):
    """Raised when the core service has not been integrated or configured."""


def load_application_service() -> Any:
    """Load the integrated core service using a narrow, documented convention.

    A may expose ``create_application_service`` or ``ApplicationService`` from
    ``finguard.core.application`` (or ``finguard.core.service``).  An explicit
    ``FINGUARD_SERVICE_FACTORY=module:attribute`` is also supported for local
    deployments.  No fallback/mock service is constructed.
    """
    candidates: list[tuple[str, str]] = []
    configured = os.getenv("FINGUARD_SERVICE_FACTORY", "")
    if configured:
        if ":" not in configured:
            raise ServiceUnavailable("FINGUARD_SERVICE_FACTORY must use module:attribute")
        candidates.append(tuple(configured.split(":", 1)))
    candidates.extend(
        (
            ("finguard.core.application", "create_application_service"),
            ("finguard.core.application", "ApplicationService"),
            ("finguard.core.service", "create_application_service"),
            ("finguard.core.service", "ApplicationService"),
        )
    )
    errors: list[str] = []
    for module_name, attr_name in candidates:
        try:
            module = importlib.import_module(module_name)
            factory = getattr(module, attr_name)
            return factory() if callable(factory) else factory
        except (ImportError, AttributeError, TypeError) as exc:
            errors.append(f"{module_name}:{attr_name} ({exc})")
    raise ServiceUnavailable(
        "ApplicationService is unavailable. Integrate finguard.core or set "
        "FINGUARD_SERVICE_FACTORY=module:attribute. Tried: " + "; ".join(errors)
    )
