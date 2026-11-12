"""Persistent list mosaics: revisions invalidate only affected covers."""

import io
from unittest.mock import AsyncMock, patch

from PIL import Image

from home_media_catalog.artwork import ArtworkError
from home_media_catalog.connectors.base import Record
from home_media_catalog.list_thumbnails import HEIGHT, WIDTH, compose


def colored_poster(rgb):
    image = Image.new("RGB", (120, 180), rgb)
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def test_mosaic_layouts_one_to_four_and_empty():
    colors = [(218, 24, 30), (27, 188, 35), (25, 54, 219), (232, 187, 27)]
    originals = [colored_poster(c) for c in colors]
    for n in range(5):
        result = compose(originals[:n])
        with Image.open(io.BytesIO(result)) as image:
            assert image.size == (WIDTH, HEIGHT)
            assert image.format == "PNG"
            assert image.mode == "RGBA"
            if n:
                actual = image.getpixel((12, 12))
                assert all(abs(actual[i] - colors[0][i]) < 28 for i in range(3))
            else:
                assert image.getpixel((0, 0))[3] == 0


def test_mosaic_tiles_have_correct_positions_for_one_to_four_images():
    colors = [(218, 24, 30), (27, 188, 35), (25, 54, 219), (232, 187, 27)]
    originals = [colored_poster(color) for color in colors]

    def matches(image, x, y, color):
        actual = image.getpixel((x, y))
        assert all(abs(actual[i] - color[i]) < 28 for i in range(3))

    for width, height in ((WIDTH, HEIGHT), (300, 450)):
        for count in (1, 2, 3, 4, 5):
            data = compose(originals[:count], width=width, height=height)
            with Image.open(io.BytesIO(data)) as image:
                matches(image, width // 4, height // 4, colors[0])
                if count == 1:
                    matches(image, width // 2, height * 3 // 4, colors[0])
                if count >= 2:
                    assert image.getpixel((width // 2, height // 2))[3] == 0
                    matches(image, width * 3 // 4, height // 4, colors[1])
                if count == 2:
                    matches(image, width * 3 // 4, height * 3 // 4, colors[1])
                if count == 3:
                    matches(image, width // 2, height * 3 // 4, colors[2])
                    assert image.getpixel((width // 8, height * 3 // 4))[3] == 0
                    assert image.getpixel((width * 7 // 8, height * 3 // 4))[3] == 0
                if count >= 4:
                    matches(image, width // 4, height * 3 // 4, colors[2])
                    matches(image, width * 3 // 4, height * 3 // 4, colors[3])


def test_mosaic_is_saved_and_replaced_at_list_revision(client, admin):
    store = client.app.state.store
    thumbnails = client.app.state.list_thumbnails
    source = store.save_source({"name": "Kodi", "type": "kodi"})
    store.apply_snapshot(
        source["id"],
        [
            Record("movie:1", "movie", "Red", poster=["https://example.org/red.jpg"]),
            Record("movie:2", "movie", "Blue", poster=["https://example.org/blue.jpg"]),
        ],
    )
    works = {item["title"]: item["id"] for item in store.media()["items"]}

    with patch.object(thumbnails.artwork, "get", new_callable=AsyncMock) as getter:
        getter.side_effect = [
            (colored_poster((210, 10, 20)), "image/png"),
            (colored_poster((210, 10, 20)), "image/png"),
            (colored_poster((10, 20, 210)), "image/png"),
            (colored_poster((10, 20, 210)), "image/png"),
        ]
        created = client.post("/api/v1/lists", json={"name": "Films"}, headers=admin)
        assert created.status_code == 201
        lid = created.json()["id"]
        original = created.json()["thumbnail"]
        assert original.endswith("/thumbnails/0.png?layout=3")
        assert client.get(original).status_code == 401
        first = client.get(original, headers=admin)
        assert first.status_code == 200
        assert first.headers["content-type"] == "image/png"
        assert "immutable" in first.headers["cache-control"]
        with patch(
            "home_media_catalog.list_thumbnails.compose", side_effect=AssertionError("cached")
        ):
            assert client.get(original, headers=admin).content == first.content

        assert (
            client.post(
                f"/api/v1/lists/{lid}/items",
                json={"media_id": works["Red"]},
                headers=admin,
            ).status_code
            == 204
        )
        added = client.get("/api/v1/lists", headers=admin).json()[0]
        assert added["thumbnail"] != original
        assert client.get(original, headers=admin).status_code == 404
        assert len(list(thumbnails.directory.glob(f"{lid}-*.png"))) == 1

        assert (
            client.post(
                f"/api/v1/lists/{lid}/items",
                json={"media_id": works["Blue"]},
                headers=admin,
            ).status_code
            == 204
        )
        changed = client.get("/api/v1/lists", headers=admin).json()[0]
        assert changed["thumbnail"] != added["thumbnail"]
        assert len(list(thumbnails.directory.glob(f"{lid}-*.png"))) == 1
        assert client.get(changed["thumbnail"], headers=admin).status_code == 200
        assert client.delete(f"/api/v1/lists/{lid}", headers=admin).status_code == 204
        assert list(thumbnails.directory.glob(f"{lid}-*.png")) == []


def test_legacy_lists_lazily_materialize_thumbnail_on_first_request(client, admin):
    store = client.app.state.store
    created = store.save_list({"name": "Existing", "description": ""})
    lid = created["id"]
    url = created["thumbnail"]
    path = client.app.state.list_thumbnails.directory / f"{lid}-0-layout3.png"
    assert not path.exists()
    assert client.get(url, headers=admin).status_code == 200
    assert path.is_file()
    assert client.get(url, headers=admin).content == path.read_bytes()


def test_manual_collection_thumbnail_recovers_after_temporary_artwork_failure(client, admin):
    store = client.app.state.store
    thumbnails = client.app.state.collection_thumbnails
    source = store.save_source({"name": "Kodi", "type": "kodi"})
    store.apply_snapshot(
        source["id"],
        [Record("movie:retry", "movie", "Recovering", poster=["https://example.org/retry.jpg"])],
    )
    media_id = store.media()["items"][0]["id"]
    collection = store.save_manual_collection("Recovering collection")
    store.add_manual_collection_items(collection["id"], [media_id])
    indexed = next(item for item in store.collection_index() if item["id"] == collection["id"])
    thumbnail_url = indexed["artwork"]
    assert thumbnail_url.endswith("?layout=5")

    with patch.object(thumbnails.artwork, "get", new_callable=AsyncMock) as getter:
        getter.side_effect = ArtworkError("Artwork temporarily unavailable")
        assert client.get(thumbnail_url, headers=admin).status_code == 200
        # Do not persist a blank mosaic for the lifetime of this revision.
        assert not list(thumbnails.directory.glob(f"{collection['id']}-*.png"))

        getter.side_effect = None
        getter.return_value = (colored_poster((195, 40, 45)), "image/png")
        recovered = client.get(thumbnail_url, headers=admin)
        assert recovered.status_code == 200
        assert list(thumbnails.directory.glob(f"{collection['id']}-*-layout5.png"))
        with Image.open(io.BytesIO(recovered.content)) as image:
            assert image.getpixel((20, 20))[0] > 120


def test_internal_collection_mosaic_rebuilds_on_add_and_remove(client, admin):
    """A 2-film mosaic must become four tiles, then three, at new revisions."""
    store = client.app.state.store
    thumbnails = client.app.state.collection_thumbnails
    source = store.save_source({"name": "Kodi", "type": "kodi"})
    colors = {
        "alpha": (210, 20, 30),
        "beta": (25, 200, 45),
        "gamma": (25, 55, 220),
        "delta": (225, 180, 25),
    }
    store.apply_snapshot(source["id"], [
        Record(f"movie:{key}", "movie", key.title(),
               poster=[f"https://example.org/{key}.jpg"])
        for key in colors
    ])
    ids = {media["title"].lower(): media["id"] for media in store.media()["items"]}
    collection = store.save_manual_collection("Scream")
    collection_id = collection["id"]

    async def poster(candidates):
        name = candidates[0].split("/")[-1].split(".")[0]
        return colored_poster(colors[name]), "image/png"

    def snapshot():
        return next(row for row in store.collection_index() if row["id"] == collection_id)

    def color_at(payload, x, y):
        with Image.open(io.BytesIO(payload)) as image:
            return image.getpixel((x, y))

    with patch.object(thumbnails.artwork, "get", new_callable=AsyncMock) as getter:
        getter.side_effect = poster
        add_two = client.post(f"/api/v1/collections/{collection_id}/items",
                              json={"media_ids": [ids["alpha"], ids["beta"]]}, headers=admin)
        assert add_two.status_code == 200
        cover_two = snapshot()["artwork"]
        data_two = client.get(cover_two, headers=admin).content
        assert color_at(data_two, 70, 350)[0] > 150  # Alpha fills the left column

        add_more = client.post(f"/api/v1/collections/{collection_id}/items",
                               json={"media_ids": [ids["gamma"], ids["delta"]]}, headers=admin)
        assert add_more.status_code == 200
        cover_four = snapshot()["artwork"]
        assert cover_four != cover_two
        assert client.get(cover_two, headers=admin).status_code == 404
        data_four = client.get(cover_four, headers=admin).content
        assert color_at(data_four, 70, 340)[0] > 150  # Delta, bottom-left
        assert len(list(thumbnails.directory.glob(f"{collection_id}-*.png"))) == 1

        removed = client.delete(f"/api/v1/collections/{collection_id}/items/{ids['alpha']}",
                                headers=admin)
        assert removed.status_code == 204
        cover_three = snapshot()["artwork"]
        assert cover_three not in {cover_two, cover_four}
        assert client.get(cover_four, headers=admin).status_code == 404
        data_three = client.get(cover_three, headers=admin).content
        with Image.open(io.BytesIO(data_three)) as image:
            assert image.size == (300, 450)
        assert len(list(thumbnails.directory.glob(f"{collection_id}-*.png"))) == 1
