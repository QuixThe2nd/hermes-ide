"""/usage balance handlers for the interactive CLI (mixed into ``HermesCLI``).
cli.py symbols are imported LAZILY inside methods — never at module load (import cycle)."""

from __future__ import annotations


class CLIBillingMixin:
    """Mixin holding interactive-CLI usage/balance display handlers."""

    # ── Shared helpers ──

    def _block_header(self, icon, title, *, rule=True) -> None:
        """Blank line, ``<icon> <bold title>`` via _cprint, then (optionally) the rule via print."""
        from cli import _cprint, _b
        print()
        _cprint(f"  {icon} {_b(title)}")
        if rule:
            print(f"  {'─' * 41}")

    def _dim(self, msg, *, icon="", lead=False) -> None:
        """Dim ``  <icon><msg>`` line via _cprint; ``lead`` prints a blank line first."""
        from cli import _cprint, _d
        if lead:
            print()
        _cprint(f"  {icon}{_d(msg)}")

    def _ok(self, msg) -> None:
        """Dim ``✓ <msg>`` success line."""
        from cli import _cprint, _DIM, _RST
        _cprint(f"  {_DIM}✓ {msg}{_RST}")

    def _try_usage_model(self):
        """Shared dollar usage model (the only source with top-up dollars); None on any failure."""
        try:
            from agent.billing_usage import build_usage_model
            return build_usage_model()
        except Exception:
            return None

    def _usage_bar_lines(self, usage, plan_name) -> list:
        """Plan + top-up bars as lines (filled = remaining). Caller picks the print fn: ordering differs per surface."""
        lines: list = []
        pb = usage.plan_bar if usage else None
        if pb is not None and pb.total_usd > 0:
            filled = max(0, min(10, round(pb.fill_fraction * 10)))
            bar = ("█" * filled) + ("░" * (10 - filled))
            pct_s = f" · {pb.pct_used}% used" if pb.pct_used is not None else ""
            label = (plan_name or "plan").ljust(8)[:8]
            lines.append(f"  {label}[{bar}]  ${pb.remaining_usd:,.2f} left of ${pb.total_usd:,.2f}{pct_s}")
        tb = usage.topup_bar if usage else None
        if tb is not None and tb.remaining_usd > 0:
            lines.append(f"  {'top-up'.ljust(8)}[{'█' * 10}]  ${tb.remaining_usd:,.2f} · never expires")
        return lines

    def _print_total_spendable(self, usage, print_fn) -> None:
        if usage and usage.has_topup and usage.total_spendable_usd is not None:
            print_fn(f"  Total spendable: ${usage.total_spendable_usd:,.2f}")

    # ── /usage — Nous balance block ──

    def _print_nous_credits_block(self) -> bool:
        """Nous dollar balance block (two bars); True if anything printed. Shared dollar model first, then
        legacy ``nous_credits_lines``. Agent-independent (TUI slash-worker has no live agent). Fail-open."""
        from cli import _cprint, _b, _d
        usage = self._try_usage_model()
        if usage is not None and usage.available:
            from agent.billing_usage import format_renews
            plan = usage.plan_name or ("Free" if usage.status == "free" else None)
            renews_display = usage.renews_display or format_renews(usage.renews_at)
            renews = f" · renews {renews_display}" if renews_display else ""
            head = [f"  {_b(f'Plan: {plan}{renews}')}"] if plan else []
            head += self._usage_bar_lines(usage, usage.plan_name)
            tail = []
            if usage.status == "free":
                tail.append(f"  {_d('> Free · free models only.')}")
            elif usage.status == "low":
                _amt = f"${usage.total_spendable_usd:,.2f}" if usage.total_spendable_usd is not None else "under $5"
                tail.append(f"  ! Low balance · {_amt} left.")
            # All via _cprint like the Plan line: print()/_cprint() flush to different buffers under
            # patch_stdout. "Total spendable" alone does not count as printed (legacy lines still follow).
            if plan:
                print()
            for ln in head:
                _cprint(ln)
            self._print_total_spendable(usage, _cprint)
            for ln in tail:
                _cprint(ln)
            if head or tail:
                return True
        from agent.account_usage import nous_credits_lines
        lines = nous_credits_lines()
        if not lines:
            return False
        print()
        for line in lines:
            print(f"  {line}")
        return True

    def _print_usage_cta(self) -> None:
        """The `/usage` call-to-action; mirrors the TUI's ``USAGE_CTA``. Nous-account only."""
        self._dim('Manage your account and balance with `hermes portal`')
