"""Chromium regression for video/music pages and the persistent music player.

Run the shipped CSS and music module with synthetic catalogue data. Click
controls through the browser's real pointer hit-testing, including a modal
album detail, route changes, and hiding/restoring the unchanged audio node.
"""

import base64
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    root = Path(__file__).resolve().parents[2] / "home_media_catalog" / "static"
    page_html = (root / "index.html").read_text()
    header = page_html[
        page_html.index('    <header class="main-header">'):
        page_html.index("    </header>") + len("    </header>")
    ]
    library = page_html[
        page_html.index('    <section id="music-page"'):
        page_html.index("  </main>", page_html.index('    <section id="music-page"'))
    ]
    detail = page_html[
        page_html.index('  <dialog id="music-detail-dialog"'):
        page_html.index('  <div id="music-player"')
    ]
    music_player = page_html[
        page_html.index('  <div id="music-player"'):page_html.index("</body>")
    ]
    music_js = (root / "music.js").read_text().replace(
        "import { tr, displayLocale } from './i18n.js';",
        "const tr = text => text; const displayLocale = () => 'fr-FR';",
    ).replace("export function startMusic()", "function startMusic()")
    music_css = (root / "music.css").read_text()
    tiny_png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
        "AAAADUlEQVQIHWP4z8DwHwAFgAI/ScLytQAAAABJRU5ErkJggg=="
    )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1200, "height": 820})
        page.route("https://music.test/", lambda route: route.fulfill(
            status=200, content_type="text/html", body="<html><body>test</body></html>"
        ))

        def serve_api(route):
            path = route.request.url
            if "/artwork/" in path:
                route.fulfill(status=200, content_type="image/png", body=tiny_png)
                return
            payload = (
                {"csrf_token": "test", "role": "admin"}
                if "/auth/session" in path else
                {"albums": 1, "artists": 1, "tracks": 2}
                if "/music/status" in path else
                {"formats": []}
                if "/music/facets" in path else
                []
                if path.endswith("/players") else
                {"items": [], "total": 0}
            )
            route.fulfill(status=200, content_type="application/json", body=__import__("json").dumps(payload))

        page.route("**/api/v1/**", serve_api)
        page.goto("https://music.test/")
        page.set_content(
            '<main id="app">' + header +
            '<section id="video-page">Bibliothèque vidéo</section>' +
            library + '</main>' + detail + music_player +
            '<div id="toast" hidden></div>'
        )
        page.add_style_tag(content=music_css)
        page.add_script_tag(content=music_js + "\nstartMusic();")

        # The actual browser URL (not a modal button) navigates libraries.
        page.locator("#music-link").click()
        page.wait_for_function("location.hash === '#music'")
        assert not page.locator("#music-page").is_hidden()
        assert page.locator("#video-page").is_hidden()
        assert page.locator("#music-link").get_attribute("aria-current") == "page"

        page.evaluate("""() => {
          const audio = document.getElementById('music-audio');
          let paused = false, position = 0, source = '';
          Object.defineProperty(audio, 'paused', {configurable:true, get:() => paused});
          Object.defineProperty(audio, 'currentTime', {configurable:true,
            get:() => position, set:value => {position=value; audio.dispatchEvent(new Event('timeupdate'));}});
          Object.defineProperty(audio, 'src', {configurable:true,
            get:() => source, set:value => {source=value;}});
          audio.pause = () => { paused=true; audio.dispatchEvent(new Event('pause')); };
          audio.play = () => { paused=false; audio.dispatchEvent(new Event('play')); return Promise.resolve(); };
          audio.load = () => {};
          state.queue = [
            {id:'song1', title:'First', artist:'Composer', album:'Album', album_id:'album-1', playable:true, file:'smb://nas/a.mp3', duration_seconds:200},
            {id:'song2', title:'Second', artist:'Composer', album:'Album', album_id:'album-1', playable:true, file:'smb://nas/b.mp3', duration_seconds:240}
          ];
          state.canonicalQueue = state.queue.slice();
          state.queueIndex = 0;
          document.getElementById('music-detail-dialog').showModal();
          showMusicPlayer();
          updateMusicTransport();
        }""")
        page.locator("#music-play-pause").click(timeout=2500)
        assert page.evaluate("document.getElementById('music-audio').paused")
        page.locator("#music-play-pause").click(timeout=2500)
        assert not page.evaluate("document.getElementById('music-audio').paused")

        page.locator("#music-next").click(timeout=2500)
        assert page.evaluate("state.queueIndex") == 1
        assert page.locator("#music-now-title").inner_text() == "Second"
        assert "/api/v1/music/albums/album-1/artwork/thumb" in page.locator("#music-now-cover").get_attribute("src")

        page.locator("#music-shuffle").click(timeout=2500)
        assert page.locator("#music-shuffle").get_attribute("aria-pressed") == "true"
        page.locator("#music-repeat").click(timeout=2500)
        page.locator("#music-repeat").click(timeout=2500)
        assert not page.locator("#music-repeat-one").is_hidden()

        page.locator("#music-volume").evaluate(
            "(node) => {node.value='35'; node.dispatchEvent(new Event('input',{bubbles:true}));}"
        )
        assert abs(page.evaluate("document.getElementById('music-audio').volume") - .35) < .001
        page.locator("#music-seek").evaluate(
            "(node) => {node.value='500'; node.dispatchEvent(new Event('change',{bubbles:true}));}"
        )
        assert abs(page.evaluate("document.getElementById('music-audio').currentTime") - 120) < 1
        page.locator("#music-mute").click(timeout=2500)
        assert page.evaluate("document.getElementById('music-audio').muted")

        page.evaluate("document.getElementById('music-detail-dialog').close()")
        page.wait_for_function(
            "document.getElementById('music-player').parentElement === document.body"
        )
        page.locator("#music-player-hide").click(timeout=2500)
        assert page.locator("#music-player").is_hidden()
        assert not page.locator("#music-player-restore").is_hidden()
        page.locator("#video-link").click(timeout=2500)
        page.wait_for_function("location.hash === '#video'")
        assert page.locator("#music-page").is_hidden()
        assert not page.locator("#video-page").is_hidden()
        page.locator("#music-player-restore").click(timeout=2500)
        assert not page.locator("#music-player").is_hidden()
        assert page.locator("#music-player-restore").is_hidden()
        assert page.evaluate("state.queueIndex") == 1
        assert not page.evaluate("document.getElementById('music-audio').paused")
        # A previous page, hidden player, and new page must share one audio.
        assert page.evaluate("document.querySelectorAll('#music-audio').length") == 1
        page.go_back()
        page.wait_for_function("location.hash === '#music'")
        assert not page.locator("#music-page").is_hidden()
        browser.close()


if __name__ == "__main__":
    main()
