import requests

# Fixed per SPEC.md - not derived from calendar, travel-day handling isn't in scope.
GURGAON_LAT = 28.4595
GURGAON_LON = 77.0266

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


def get_tomorrow_forecast() -> dict:
    resp = requests.get(
        FORECAST_URL,
        params={
            "latitude": GURGAON_LAT,
            "longitude": GURGAON_LON,
            "daily": "temperature_2m_max,temperature_2m_min,apparent_temperature_max,"
                     "precipitation_probability_max,precipitation_sum",
            "timezone": "Asia/Kolkata",
            "forecast_days": 2,
        },
        timeout=15,
    )
    resp.raise_for_status()
    daily = resp.json()["daily"]
    return {
        "date": daily["time"][1],
        "temp_max": daily["temperature_2m_max"][1],
        "temp_min": daily["temperature_2m_min"][1],
        "feels_like_max": daily["apparent_temperature_max"][1],
        "precip_probability": daily["precipitation_probability_max"][1],
        "precip_mm": daily["precipitation_sum"][1],
    }


def fabric_constraints(forecast: dict) -> dict:
    hot = forecast["feels_like_max"] >= 35
    warm = 28 <= forecast["feels_like_max"] < 35
    cold = forecast["temp_min"] < 15
    rainy = forecast["precip_probability"] >= 50

    recommended, avoid = set(), set()

    if hot:
        recommended |= {"cotton", "linen", "chiffon", "georgette", "light silk"}
        avoid |= {"heavy silk", "velvet", "heavily embellished"}
    elif cold:
        recommended |= {"heavy silk", "banarasi", "velvet blend"}
        avoid |= {"chiffon", "georgette", "linen"}
    elif warm:
        recommended |= {"cotton", "silk blend", "georgette"}
        avoid |= {"velvet", "heavily embellished"}
    else:
        recommended |= {"cotton", "silk", "silk blend"}

    if rainy:
        recommended |= {"synthetic blend", "wash-friendly cotton"}
        avoid |= {"delicate silk", "heavy zari work"}

    return {
        "recommended_fabrics": sorted(recommended),
        "avoid_fabrics": sorted(avoid),
    }


def get_weather_constraints() -> dict:
    forecast = get_tomorrow_forecast()
    return {**forecast, **fabric_constraints(forecast)}


if __name__ == "__main__":
    result = get_weather_constraints()
    for key, value in result.items():
        print(f"{key}: {value}")
