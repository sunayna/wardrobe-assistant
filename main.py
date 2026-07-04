import datetime

from confirm import confirm_today, record_recommendation
from context import get_context
from ranking import rank_candidates
from wardrobe import query_wardrobe
from weather import get_weather_constraints


def deliver(result: dict, occasion_ctx, weather_ctx: dict) -> None:
    top = result["top_pick"]
    print(
        f"\nTomorrow ({weather_ctx['date']}): {occasion_ctx.occasion}, "
        f"feels like {weather_ctx['feels_like_max']}°C, "
        f"{weather_ctx['precip_probability']}% chance of rain.\n"
    )
    print(f"Wear: {top['fabric']} ({top['color']}) — {top['reasoning']}")
    if result["alternates"]:
        print("\nAlternates:")
        for alt in result["alternates"]:
            print(f"  - {alt['fabric']} ({alt['color']}) — {alt['reasoning']}")


def run() -> dict:
    confirm_today()

    occasion_ctx = get_context()
    weather_ctx = get_weather_constraints()
    candidates = query_wardrobe(occasion_ctx.formality, weather_ctx["avoid_fabrics"])

    if not candidates:
        print(
            "No sarees match tomorrow's occasion/weather even after relaxing "
            "filters — you may need to tag more of your catalog."
        )
        return {}

    result = rank_candidates(occasion_ctx, weather_ctx, candidates)
    deliver(result, occasion_ctx, weather_ctx)

    tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    record_recommendation(result["top_pick"]["photo_id"], tomorrow)

    return result


if __name__ == "__main__":
    run()
