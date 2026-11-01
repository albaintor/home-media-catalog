"""Browser regression: music controls must accept real clicks inside nested modals.

Uses the actual shipped HTML/CSS/JS with a mocked audio transport. No network
or audio hardware is needed; Chromium verifies the hit target is not inert.
"""

from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    root = Path(__file__).resolve().parents[2] / "home_media_catalog" / "static"
    page_html = (root / "index.html").read_text()
    music_html = page_html[page_html.index('  <div id="music-player"') : page_html.index("</body>")]
    music_js = (root / "music.js").read_text().replace(
        "import { tr, displayLocale } from './i18n.js';",
        "const tr = text => text; const displayLocale = () => 'fr-FR';",
    ).replace("export function startMusic()", "function startMusic()")
    music_css = (root / "music.css").read_text()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1200, "height": 820})
        page.route("https://music.test/", lambda route: route.fulfill(
            status=200, content_type="text/html", body="<html><body>test</body></html>"
        ))
        page.goto("https://music.test/")
        page.set_content(
            "<button id='music-button'>Musique</button>"
            "<input id='music-search'><select id='music-format'></select>"
            "<button id='music-more'>Plus</button>"
            "<dialog id='music-dialog'><button id='album-button'>Album</button></dialog>"
            "<dialog id='music-detail-dialog'><button id='detail-button'>Détail</button></dialog>"
            + music_html
        )
        page.add_style_tag(content=music_css)
        page.add_script_tag(content=music_js + "\nstartMusic();")
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
            {id:'song1', title:'First', artist:'Composer', album:'Album', playable:true, file:'smb://nas/a.mp3', duration_seconds:200},
            {id:'song2', title:'Second', artist:'Composer', album:'Album', playable:true, file:'smb://nas/b.mp3', duration_seconds:240}
          ];
          state.canonicalQueue = state.queue.slice();
          state.queueIndex = 0;
          document.getElementById('music-dialog').showModal();
          document.getElementById('music-detail-dialog').showModal();
          showMusicPlayer();
          updateMusicTransport();
        }""")
        # Pointer hit-testing is the regression: a popover outside a modal
        # looked visible but clicks landed on the dialog underneath.
        page.locator("#music-play-pause").click(timeout=2500)
        assert page.evaluate("document.getElementById('music-audio').paused")
        page.locator("#music-play-pause").click(timeout=2500)
        assert not page.evaluate("document.getElementById('music-audio').paused")

        page.locator("#music-next").click(timeout=2500)
        assert page.evaluate("state.queueIndex") == 1
        assert page.locator("#music-now-title").inner_text() == "Second"
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
        page.wait_for_function("document.getElementById('music-player').parentElement.id === 'music-dialog'")
        page.locator("#music-play-pause").click(timeout=2500)
        assert page.evaluate("document.getElementById('music-audio').paused")
        page.evaluate("document.getElementById('music-dialog').close()")
        page.wait_for_function("document.getElementById('music-player').parentElement === document.body")
        browser.close()


if __name__ == "__main__":
    main()
