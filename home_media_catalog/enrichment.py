"""Optional TMDB credits, portraits and trailers, matched only by known IDs."""

import re
from contextlib import asynccontextmanager

import httpx

from .connectors.base import ConnectorError
from .people import category, youtube_url


@asynccontextmanager
async def tmdb_connection(token: str):
    token = token.strip().removeprefix("Bearer ").strip()
    if not token:
        raise ConnectorError("Renseignez TMDB_API_TOKEN dans .env puis recréez le conteneur.")
    api_key = bool(re.fullmatch(r"[a-fA-F0-9]{32}", token))
    try:
        async with httpx.AsyncClient(
            base_url="https://api.themoviedb.org/3/",
            headers={} if api_key else {"Authorization": "Bearer " + token},
            params={"api_key": token} if api_key else {},
            timeout=20,
            trust_env=False,
        ) as client:

            async def get(path, **params):
                response = await client.get(path, params=params)
                response.raise_for_status()
                return response.json()

            yield get
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (401, 403):
            message = "TMDB refuse la clé. Vérifiez le jeton de lecture API ou la clé API v3 dans TMDB_API_TOKEN."
        elif exc.response.status_code == 404:
            message = "Ce média est introuvable sur TMDB avec ses identifiants importés."
        elif exc.response.status_code == 429:
            message = "TMDB limite temporairement les requêtes. Réessayez dans quelques instants."
        else:
            message = "TMDB est temporairement indisponible. Réessayez plus tard."
        raise ConnectorError(message) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise ConnectorError(
            "Connexion TMDB impossible. Vérifiez l'accès Internet et les DNS du conteneur."
        ) from exc


async def probe_tmdb(token: str) -> None:
    async with tmdb_connection(token) as get:
        data = await get("configuration")
        if not isinstance(data, dict) or not isinstance(data.get("images"), dict):
            raise ConnectorError("TMDB n'a pas renvoyé une configuration valide.")


def tmdb_poster_reference(path: str | None) -> str | None:
    """Only TMDB CDN paths from provider responses are accepted."""
    if isinstance(path, str) and re.fullmatch(r"/[A-Za-z0-9_.-]{1,150}", path):
        return "https://image.tmdb.org/t/p/w500" + path
    return None


async def resolve_tmdb_poster(detail: dict, token: str) -> str | None:
    """Resolve known IDs without title matching; avoid credits/video downloads."""
    kind = {"movie": "movie", "tvshow": "tv"}.get(detail.get("kind"))
    if not kind:
        return None
    ids = detail.get("identifiers") or {}
    tmdb_ids = [v for v in ids.get("tmdb", []) if isinstance(v, str) and re.fullmatch(r"[0-9]{1,24}", v)]
    imdb_ids = [v for v in ids.get("imdb", []) if isinstance(v, str) and re.fullmatch(r"tt[0-9]{5,12}", v)]
    if len(tmdb_ids) != 1 and len(imdb_ids) != 1:
        return None
    async with tmdb_connection(token) as get:
        if len(tmdb_ids) == 1:
            identifier = tmdb_ids[0]
        else:
            found = await get("find/" + imdb_ids[0], external_source="imdb_id")
            matches = found.get("movie_results" if kind == "movie" else "tv_results") or []
            if len(matches) != 1:
                return None
            identifier = str(matches[0]["id"])
        data = await get(f"{kind}/{identifier}", language="fr-FR")
        return tmdb_poster_reference(data.get("poster_path"))


async def enrich_tmdb(detail: dict, token: str) -> dict:
    kind = {"movie": "movie", "tvshow": "tv"}.get(detail["kind"])
    if not kind:
        raise ConnectorError("TMDB enrichment currently supports movies and TV shows.")
    ids = detail["identifiers"]
    tmdb = ids.get("tmdb", [])
    try:
        async with tmdb_connection(token) as get:
            if len(tmdb) == 1 and re.fullmatch(r"\d+", tmdb[0]):
                tmdb_id = tmdb[0]
            else:
                imdb = ids.get("imdb", [])
                if len(imdb) != 1 or not re.fullmatch(r"tt\d+", imdb[0]):
                    raise ConnectorError(
                        "Un identifiant IMDb ou TMDB unique est nécessaire. Synchronisez la source Kodi ou réimportez CLZ si cet identifiant manque."
                    )
                found = await get("find/" + imdb[0], external_source="imdb_id")
                matches = found.get("movie_results" if kind == "movie" else "tv_results", [])
                if len(matches) != 1:
                    raise ConnectorError(
                        "TMDB ne trouve pas de correspondance unique avec l'identifiant IMDb de ce média."
                    )
                tmdb_id = str(matches[0]["id"])
            data = await get(
                f"{kind}/{tmdb_id}",
                append_to_response="credits,videos",
                language="fr-FR",
                include_video_language="fr,en,null",
            )
            people = []
            for group in ("cast", "crew"):
                for row in data.get("credits", {}).get(group, []):
                    if not row.get("name"):
                        continue
                    path = row.get("profile_path") or ""
                    photo = (
                        "https://image.tmdb.org/t/p/w185" + path
                        if re.fullmatch(r"/[A-Za-z0-9_.-]+", path)
                        else None
                    )
                    people.append(
                        {
                            "name": row["name"],
                            "role": row.get("character") if group == "cast" else row.get("job"),
                            "category": "cast"
                            if group == "cast"
                            else category(row.get("job") or "crew"),
                            "order": row.get("order"),
                            "tmdb_id": str(row["id"]),
                            "photos": [photo] if photo else [],
                        }
                    )
            videos = sorted(
                data.get("videos", {}).get("results", []),
                key=lambda v: (v.get("iso_639_1") != "fr", not v.get("official", False)),
            )
            trailers = [
                url
                for v in videos
                if v.get("site") == "YouTube" and v.get("type") == "Trailer"
                if (url := youtube_url("https://youtu.be/" + str(v.get("key", ""))))
            ]
            # TV details already contain season-specific poster paths. Store
            # only validated TMDB image URLs alongside the existing credits,
            # with no additional upstream requests or exposed API credentials.
            season_posters = {}
            if kind == "tv":
                for season in data.get("seasons") or []:
                    if not isinstance(season, dict):
                        continue
                    number, path = season.get("season_number"), season.get("poster_path")
                    if (
                        type(number) is int
                        and 0 <= number <= 10000
                        and isinstance(path, str)
                        and re.fullmatch(r"/[A-Za-z0-9_.-]+", path)
                    ):
                        season_posters[str(number)] = "https://image.tmdb.org/t/p/w342" + path
            return {
                "people": people,
                "trailers": list(dict.fromkeys(trailers)),
                "tmdb_id": tmdb_id,
                **({"poster": poster} if (poster := tmdb_poster_reference(data.get("poster_path"))) else {}),
                **({"season_posters": season_posters} if kind == "tv" else {}),
            }
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ConnectorError(
            "TMDB enrichment failed. Check the API token, DNS and TMDB availability."
        ) from exc


async def enrich_tmdb_season_guide(detail: dict, season: int, token: str) -> list[dict]:
    """Read-only episode metadata for a physical season without Kodi files.

    Episode identities are not written to the catalogue, so these reference
    entries cannot be mistaken for playable or watched Kodi episodes.
    """
    if detail.get("kind") != "tvshow" or not 0 <= season <= 10000:
        raise ConnectorError("A valid TV season is required.")
    ids = detail.get("identifiers") or {}
    tmdb = ids.get("tmdb") or []
    imdb = ids.get("imdb") or []
    async with tmdb_connection(token) as get:
        if len(tmdb) == 1 and isinstance(tmdb[0], str) and re.fullmatch(r"[0-9]{1,24}", tmdb[0]):
            identifier = tmdb[0]
        elif len(imdb) == 1 and isinstance(imdb[0], str) and re.fullmatch(r"tt[0-9]{5,12}", imdb[0]):
            found = await get("find/" + imdb[0], external_source="imdb_id")
            matches = found.get("tv_results") or []
            if len(matches) != 1:
                raise ConnectorError("TMDB ne trouve pas cette série avec son identifiant IMDb.")
            identifier = str(matches[0]["id"])
        else:
            raise ConnectorError("Identifiant TMDB ou IMDb unique requis pour cette série.")
        data = await get(f"tv/{identifier}/season/{season}", language="fr-FR")
    result = []
    for episode in data.get("episodes") or []:
        if not isinstance(episode, dict):
            continue
        number = episode.get("episode_number")
        if type(number) is not int or not 0 <= number <= 10000:
            continue
        still = episode.get("still_path")
        still_url = (
            "https://image.tmdb.org/t/p/w300" + still
            if isinstance(still, str) and re.fullmatch(r"/[A-Za-z0-9_.-]{1,150}", still)
            else None
        )
        runtime = episode.get("runtime")
        result.append({
            "episode": number,
            "title": str(episode.get("name") or f"Épisode {number}")[:500],
            "plot": str(episode.get("overview") or "")[:5000],
            "runtime_minutes": runtime if type(runtime) is int and 0 < runtime < 10000 else None,
            "still": still_url,
        })
    return sorted(result, key=lambda item: item["episode"])
