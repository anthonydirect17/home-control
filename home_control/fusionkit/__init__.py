"""The Fusion kit's Python side, shared by every app behind the Bridge.

- health: the health feed (schema fusion-health/1) every app answers at /api/health.
- link:   checking that a request came through the Bridge (fusion-link/1), and its one-time
          PIN confirmations for sensitive actions.
- dpapi:  Windows DPAPI, used by link for the shared secret.

Standard library only. An app copies this folder into its own code (it is small and changes
rarely); the version below says which copy it has.
"""

__version__ = "1.0.0"
