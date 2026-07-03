import datetime
import re

from langchain_ollama import ChatOllama

RANKING_PROMPT = """You are picking a saree to wear tomorrow. Given the occasion,
weather, and a numbered list of candidate sarees below, pick the single best option
and up to two alternates.

Occasion: {occasion} (formality {formality}/5, {time_of_day}, {indoor_outdoor})
Weather: feels like {feels_like_max}C, {precip_probability}% chance of rain

Candidates:
{candidates_text}

Rank by: how well the fabric/formality fits the occasion, how well the fabric suits
the weather (avoid: {avoid_fabrics}), and freshness (prefer sarees worn longer ago or
never worn - "never worn" beats any specific day count).

Respond with ONLY plain text in EXACTLY this format, nothing else - one line per
label, using the candidate's number after each label:

TOP: <number>
TOP_REASON: <one sentence>
ALT: <number>
ALT_REASON: <one sentence>
ALT: <number>
ALT_REASON: <one sentence>

Pick at most 2 ALT lines (fewer if there aren't enough other candidates). Do not use
JSON, markdown, or any other formatting."""


def _format_candidates(candidates: list[dict]) -> str:
    today = datetime.date.today()
    lines = []
    for i, c in enumerate(candidates, start=1):
        if c.get("last_worn_date"):
            days_ago = (today - datetime.date.fromisoformat(c["last_worn_date"])).days
            freshness = f"last worn {days_ago} days ago"
        else:
            freshness = "never worn"
        lines.append(
            f"{i}. {c['fabric']} ({c['color']}), formality {c['formality']}/5, "
            f"tags: {c['occasion_tags']}, {freshness}"
        )
    return "\n".join(lines)


def _parse_ranking(raw: str) -> tuple[int | None, str, list[tuple[int, str]]]:
    top_index, top_reason = None, ""
    alt_entries: list[tuple[int, str]] = []
    pending_alt_index = None

    for line in raw.splitlines():
        line = line.strip()
        upper = line.upper()
        if upper.startswith("TOP_REASON:"):
            top_reason = line.split(":", 1)[1].strip()
        elif upper.startswith("TOP:"):
            match = re.search(r"\d+", line)
            top_index = int(match.group()) if match else None
        elif upper.startswith("ALT_REASON:"):
            reason = line.split(":", 1)[1].strip()
            if pending_alt_index is not None:
                alt_entries.append((pending_alt_index, reason))
                pending_alt_index = None
        elif upper.startswith("ALT:"):
            match = re.search(r"\d+", line)
            pending_alt_index = int(match.group()) if match else None

    return top_index, top_reason, alt_entries


def rank_candidates(occasion_ctx, weather_ctx: dict, candidates: list[dict]) -> dict:
    if not candidates:
        raise ValueError("No candidates to rank.")

    prompt = RANKING_PROMPT.format(
        occasion=occasion_ctx.occasion,
        formality=occasion_ctx.formality,
        time_of_day=occasion_ctx.time_of_day,
        indoor_outdoor=occasion_ctx.indoor_outdoor,
        feels_like_max=weather_ctx["feels_like_max"],
        precip_probability=weather_ctx["precip_probability"],
        avoid_fabrics=", ".join(weather_ctx["avoid_fabrics"]),
        candidates_text=_format_candidates(candidates),
    )

    model = ChatOllama(model="llama3.2", temperature=0)
    response = model.invoke(prompt)
    top_index, top_reason, alt_entries = _parse_ranking(response.content)

    def resolve(index: int | None, reason: str) -> dict:
        idx = (index - 1) if index is not None else 0
        if not (0 <= idx < len(candidates)):
            idx = 0  # model gave a missing/out-of-range index - fall back safely
        return {**candidates[idx], "reasoning": reason}

    top_pick = resolve(top_index, top_reason)
    alternates = [resolve(i, r) for i, r in alt_entries if i != top_index]
    return {"top_pick": top_pick, "alternates": alternates}


if __name__ == "__main__":
    from context import get_context
    from wardrobe import query_wardrobe
    from weather import get_weather_constraints

    occasion_ctx = get_context()
    weather_ctx = get_weather_constraints()
    candidates = query_wardrobe(occasion_ctx.formality, weather_ctx["avoid_fabrics"])

    result = rank_candidates(occasion_ctx, weather_ctx, candidates)
    top = result["top_pick"]
    print(f"\nTop pick: {top['fabric']} ({top['color']}) - {top['reasoning']}")
    print("\nAlternates:")
    for alt in result["alternates"]:
        print(f"  {alt['fabric']} ({alt['color']}) - {alt['reasoning']}")
