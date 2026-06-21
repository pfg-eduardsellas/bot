import urllib.request
from typing import List, Optional

AXE_VERSION = "4.9.1"
AXE_CDN = f"https://cdnjs.cloudflare.com/ajax/libs/axe-core/{AXE_VERSION}/axe.min.js"

_axe_script: Optional[str] = None


def _load_axe_script() -> Optional[str]:
    global _axe_script
    if _axe_script is not None:
        return _axe_script
    try:
        with urllib.request.urlopen(AXE_CDN, timeout=15) as resp:
            _axe_script = resp.read().decode("utf-8")
        return _axe_script
    except Exception as e:
        print(f"[Accessibility] Failed to load axe-core from CDN: {e}")
        return None


async def setup_axe(page) -> bool:
    """Register axe-core as an init script so it's injected on every page navigation."""
    script = _load_axe_script()
    if script is None:
        return False
    await page.add_init_script(script=script)
    return True


async def run_full_scan(page) -> List[dict]:
    """Run axe on the entire page and return violations."""
    return await _axe_evaluate(page, include=None)


async def run_partial_scan(page, selectors: List[str]) -> List[dict]:
    """Run axe scoped to elements matching the given selectors."""
    if not selectors:
        return []
    return await _axe_evaluate(page, include=selectors)


async def _axe_evaluate(page, include: Optional[List[str]]) -> List[dict]:
    try:
        violations = await page.evaluate(
            """
            async (include) => {
                if (typeof axe === 'undefined') return [];
                try {
                    const context = include
                        ? { include: include.map(s => [s]) }
                        : document;
                    const result = await axe.run(context, { runOnly: ['wcag2a', 'wcag2aa', 'best-practice'] });
                    return result.violations.map(v => ({
                        rule_id: v.id,
                        impact: v.impact,
                        description: v.description,
                        help_url: v.helpUrl,
                        nodes: v.nodes.map(n => ({
                            target: n.target,
                            html: n.html,
                            failure_summary: n.failureSummary
                        }))
                    }));
                } catch (e) {
                    return [];
                }
            }
        """,
            include,
        )
        return violations or []
    except Exception:
        return []
