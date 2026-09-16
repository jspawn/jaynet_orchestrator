"""code.delegate — legacy alias of specialist.delegate.

The delegate tool is no longer coding-only (its `strength` parameter routes
coding/research/security/multi-step work to specialist models), so it moved to
tools/specialist/delegate.py under the name specialist.delegate. This hidden
alias keeps the old name working — identical behavior, same class — so old
prompts, evals, skills and saved chats don't break. New text should say
specialist.delegate.
"""

from __future__ import annotations

from tools.specialist.delegate import SpecialistDelegate


class CodeDelegate(SpecialistDelegate):
    name = "code.delegate"
    hidden = True
