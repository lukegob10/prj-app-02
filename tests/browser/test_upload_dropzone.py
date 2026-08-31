from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import FilePayload, Page, expect

DROPZONE_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "agora"
    / "portal"
    / "static"
    / "portal"
    / "upload-dropzone.js"
)
FOUNDATION_CSS = DROPZONE_SCRIPT.with_name("foundation.css")

pytestmark = [pytest.mark.browser, pytest.mark.only_browser("chromium")]

_UPLOAD_MARKUP = """
<!doctype html>
<html lang="en">
  <body>
    <form>
      <div class="portal-form-field" data-upload-widget>
        <div class="portal-dropzone" data-upload-dropzone>
          <label for="id_files">Dashboard files</label>
          <span data-upload-browse-surface>Click anywhere to choose files</span>
          <input class="portal-dropzone__input" id="id_files" name="files"
                 type="file" multiple data-upload-input>
        </div>
        <p data-upload-summary></p>
        <p data-upload-announcement></p>
        <section class="portal-upload-queue" data-upload-queue hidden>
          <button type="button" data-upload-clear>Clear all</button>
          <ul data-upload-list></ul>
        </section>
      </div>
    </form>
  </body>
</html>
"""


def _open_upload_widget(page: Page) -> None:
    page.set_content(_UPLOAD_MARKUP)
    page.add_style_tag(path=str(FOUNDATION_CSS))
    page.add_script_tag(path=str(DROPZONE_SCRIPT))


def test_file_chooser_batches_merge_and_same_name_replaces_queued_file(page: Page) -> None:
    _open_upload_widget(page)
    file_input = page.locator("[data-upload-input]")

    initial_files: list[FilePayload] = [
        {
            "name": "dashboard.html",
            "mimeType": "text/html",
            "buffer": b"<html></html>",
        },
        {
            "name": "sales.csv",
            "mimeType": "text/csv",
            "buffer": b"old",
        },
    ]
    replacement: FilePayload = {
        "name": "SALES.csv",
        "mimeType": "text/csv",
        "buffer": b"newer data",
    }
    file_input.set_input_files(initial_files)
    file_input.set_input_files(replacement)

    expect(page.locator("[data-upload-list] strong")).to_have_text(["dashboard.html", "SALES.csv"])
    expect(page.locator("[data-upload-dropzone]")).to_have_css("display", "grid")
    expect(page.locator("[data-upload-dropzone]")).to_have_css("border-top-style", "dashed")
    expect(page.locator("[data-upload-queue]")).to_be_visible()
    expect(page.locator("[data-upload-summary]")).to_contain_text("2 files selected")
    expect(page.locator("[data-upload-summary]")).to_contain_text("One HTML entry point selected")
    expect(page.locator("[data-upload-announcement]")).to_contain_text(
        "SALES.csv replaced the queued file with the same name"
    )
    expect(page.locator("[data-upload-announcement]")).to_be_visible()
    assert file_input.evaluate("input => input.validationMessage") == ""
    submitted_files = file_input.evaluate(
        "input => Array.from(input.files, file => ({name: file.name, size: file.size}))"
    )
    assert submitted_files == [
        {"name": "dashboard.html", "size": 13},
        {"name": "SALES.csv", "size": 10},
    ]


def test_clicking_dropzone_opens_multiselect_file_chooser(page: Page) -> None:
    _open_upload_widget(page)
    file_input = page.locator("[data-upload-input]")
    dropzone = page.locator("[data-upload-dropzone]")
    expect(dropzone).to_have_css("cursor", "pointer")
    expect(file_input).to_have_css("position", "absolute")
    assert dropzone.evaluate(
        """element => {
          const bounds = element.getBoundingClientRect();
          return document.elementFromPoint(bounds.left + 12, bounds.top + 12).tagName;
        }"""
    ) == "INPUT"

    with page.expect_file_chooser() as chooser_info:
        dropzone.click(position={"x": 12, "y": 12})
    chooser_info.value.set_files(
        [
            {
                "name": "dashboard.html",
                "mimeType": "text/html",
                "buffer": b"<html></html>",
            },
            {
                "name": "sales.csv",
                "mimeType": "text/csv",
                "buffer": b"month,total\nJan,10\n",
            },
        ]
    )

    assert file_input.get_attribute("multiple") == ""
    expect(page.locator("[data-upload-list] strong")).to_have_text(
        ["dashboard.html", "sales.csv"]
    )
    expect(page.locator("[data-upload-summary]")).to_contain_text("2 files selected")


def test_portal_shell_prevents_native_file_open_without_upload_widget(page: Page) -> None:
    page.set_content("<!doctype html><html lang='en'><body><main>Projects</main></body></html>")
    page.add_script_tag(path=str(DROPZONE_SCRIPT))

    prevented = page.locator("body").evaluate(
        """body => {
          const transfer = new DataTransfer();
          transfer.items.add(new File(['<html></html>'], 'dashboard.html', {type: 'text/html'}));
          const dragover = new DragEvent('dragover', {
            bubbles: true,
            cancelable: true,
            dataTransfer: transfer,
          });
          const drop = new DragEvent('drop', {
            bubbles: true,
            cancelable: true,
            dataTransfer: transfer,
          });
          body.dispatchEvent(dragover);
          body.dispatchEvent(drop);
          return {
            dragoverPrevented: dragover.defaultPrevented,
            dropPrevented: drop.defaultPrevented,
            url: window.location.href,
          };
        }"""
    )

    assert prevented == {
        "dragoverPrevented": True,
        "dropPrevented": True,
        "url": "about:blank",
    }


def test_file_chooser_recognizes_new_files_when_window_focus_returns_first(page: Page) -> None:
    """The native chooser may restore window focus before it emits ``change``."""

    _open_upload_widget(page)
    file_input = page.locator("[data-upload-input]")
    file_input.set_input_files(
        {
            "name": "dashboard.html",
            "mimeType": "text/html",
            "buffer": b"old",
        }
    )

    file_input.evaluate(
        """input => {
          input.addEventListener('click', event => event.preventDefault(), {once: true});
          input.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
          const transfer = new DataTransfer();
          transfer.items.add(new File(['new dashboard'], 'dashboard.html', {type: 'text/html'}));
          input.files = transfer.files;
          window.dispatchEvent(new Event('focus'));
        }"""
    )
    page.wait_for_timeout(10)
    assert file_input.evaluate("input => input.files[0].size") == 13
    file_input.dispatch_event("change")

    submitted_files = file_input.evaluate(
        "input => Array.from(input.files, file => ({name: file.name, size: file.size}))"
    )
    assert submitted_files == [{"name": "dashboard.html", "size": 13}]
    expect(page.locator("[data-upload-announcement]")).to_contain_text(
        "dashboard.html replaced the queued file with the same name"
    )


def test_drop_batches_append_files_and_queue_controls_remove_them(page: Page) -> None:
    _open_upload_widget(page)
    dropzone = page.locator("[data-upload-dropzone]")
    dropzone.evaluate(
        """(element) => {
          const transfer = new DataTransfer();
          transfer.items.add(new File(['<html></html>'], 'dashboard.html', {type: 'text/html'}));
          transfer.items.add(new File(['body{}'], 'theme.css', {type: 'text/css'}));
          element.dispatchEvent(new DragEvent('drop', {
            bubbles: true,
            cancelable: true,
            dataTransfer: transfer,
          }));
        }"""
    )
    dropzone.evaluate(
        """(element) => {
          const transfer = new DataTransfer();
          transfer.items.add(new File(['new theme'], 'theme.css', {type: 'text/css'}));
          transfer.items.add(new File(['png'], 'logo.png', {type: 'image/png'}));
          element.dispatchEvent(new DragEvent('drop', {
            bubbles: true,
            cancelable: true,
            dataTransfer: transfer,
          }));
        }"""
    )

    expect(page.locator("[data-upload-list] strong")).to_have_text(
        ["dashboard.html", "theme.css", "logo.png"]
    )
    submitted_files = page.locator("[data-upload-input]").evaluate(
        "input => Array.from(input.files, file => ({name: file.name, size: file.size}))"
    )
    assert submitted_files == [
        {"name": "dashboard.html", "size": 13},
        {"name": "theme.css", "size": 9},
        {"name": "logo.png", "size": 3},
    ]

    page.get_by_role("button", name="Remove theme.css").click()
    expect(page.locator("[data-upload-list] strong")).to_have_text(["dashboard.html", "logo.png"])
    page.get_by_role("button", name="Clear all").click()
    expect(page.locator("[data-upload-summary]")).to_have_text("No files selected.")
    assert page.locator("[data-upload-input]").evaluate("input => input.validationMessage") != ""
    assert page.locator("[data-upload-queue]").get_attribute("hidden") == ""


def test_file_drag_stays_purple_over_focused_nested_dropzone_content(page: Page) -> None:
    _open_upload_widget(page)
    dropzone = page.locator("[data-upload-dropzone]")
    page.locator("[data-upload-input]").focus()

    dropzone.evaluate(
        """element => {
          const transfer = new DataTransfer();
          transfer.items.add(new File(['data'], 'sales.csv', {type: 'text/csv'}));
          const options = {
            bubbles: true,
            cancelable: true,
            dataTransfer: transfer,
          };
          element.dispatchEvent(new DragEvent('dragenter', options));
          const label = element.querySelector('label');
          label.dispatchEvent(new DragEvent('dragenter', options));
          label.dispatchEvent(new DragEvent('dragleave', options));
        }"""
    )

    expect(dropzone).to_have_class("portal-dropzone is-dragover")
    expect(dropzone).to_have_css("border-top-color", "rgb(109, 40, 217)")
    expect(dropzone).to_have_css("background-color", "rgb(245, 243, 255)")

    dropzone.evaluate(
        """element => {
          const transfer = new DataTransfer();
          element.dispatchEvent(new DragEvent('drop', {
            bubbles: true,
            cancelable: true,
            dataTransfer: transfer,
          }));
        }"""
    )
    expect(dropzone).not_to_have_class("is-dragover")
