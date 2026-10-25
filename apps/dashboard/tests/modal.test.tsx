/**
 * The accessible Modal: dialog role and labelling in its markup, and the focus-trap rule
 * (trapTarget), which decides where Tab and Shift+Tab go at the dialog's edges.
 */

import assert from "node:assert/strict";
import { test } from "node:test";
import { FOCUSABLE_SELECTOR, Modal, trapTarget } from "../src/components/Modal";
import { render, tags, textOf, withAttr } from "./support/render";

const noop = () => undefined;

test("a closed modal renders nothing", () => {
  assert.equal(render(<Modal open={false} onClose={noop} title="Approve?" />), "");
});

test("an open modal is a dialog, modal, labelled by its title and described by its description", () => {
  const html = render(
    <Modal open onClose={noop} title="Approve this exact action?" description="Nothing runs until you decide." footer={<button type="button">Approve</button>}>
      <p>rollback_deployment on checkout</p>
    </Modal>,
  );
  const dialog = withAttr(html, "role", "dialog");
  assert.equal(dialog.length, 1);
  const attrs = dialog[0].attrs;
  assert.equal(attrs["aria-modal"], "true");
  assert.equal(attrs.tabindex, "-1");
  const heading = tags(html, "h2")[0];
  assert.equal(attrs["aria-labelledby"], heading.attrs.id);
  const description = tags(html, "p").find((p) => p.attrs.class === "modal-description");
  assert.equal(attrs["aria-describedby"], description?.attrs.id);
  assert.notEqual(heading.attrs.id, description?.attrs.id);
  assert.match(textOf(html), /^Approve this exact action\? Nothing runs until you decide\. rollback_deployment on checkout Approve$/);
});

test("a dismissible modal has a labelled close button; a non-dismissible one has none", () => {
  const html = render(<Modal open onClose={noop} title="Details" closeLabel="Close details" />);
  const close = tags(html, "button").find((b) => b.attrs["aria-label"] === "Close details");
  assert.ok(close);
  assert.equal(close.attrs.type, "button");
  const locked = render(<Modal open onClose={noop} title="Decide" dismissible={false} footer={<button type="button">Reject</button>} />);
  assert.equal(tags(locked, "button").length, 1);
  assert.equal(withAttr(locked, "aria-describedby").length, 0);
});

test("sizes map to classes", () => {
  assert.match(render(<Modal open onClose={noop} title="x" size="large" />), /class="modal modal-large"/);
});

test("hostile text in the title and body is escaped", () => {
  const html = render(
    <Modal open onClose={noop} title={`<script>alert(1)</script>`}>
      {`<a href="javascript:alert(1)">click</a>`}
    </Modal>,
  );
  assert.doesNotMatch(html, /<script|<a href/);
  assert.equal(withAttr(html, "href").length, 0);
});

test("focus trap: Tab on the last element wraps to the first, Shift+Tab on the first wraps to the last", () => {
  assert.equal(trapTarget(2, 3, false), 0);
  assert.equal(trapTarget(0, 3, true), 2);
});

test("focus trap: inside the dialog the browser moves focus normally", () => {
  assert.equal(trapTarget(0, 3, false), null);
  assert.equal(trapTarget(1, 3, false), null);
  assert.equal(trapTarget(1, 3, true), null);
  assert.equal(trapTarget(2, 3, true), null);
});

test("focus trap: from the dialog itself focus enters at an edge; with nothing focusable it stays", () => {
  assert.equal(trapTarget(-1, 3, false), 0);
  assert.equal(trapTarget(-1, 3, true), 2);
  assert.equal(trapTarget(-1, 0, false), -1);
  assert.equal(trapTarget(0, 1, false), 0);
  assert.equal(trapTarget(0, 1, true), 0);
});

test("what counts as focusable excludes disabled controls and tabindex=-1", () => {
  assert.match(FOCUSABLE_SELECTOR, /button:not\(\[disabled\]\)/);
  assert.match(FOCUSABLE_SELECTOR, /a\[href\]/);
  assert.match(FOCUSABLE_SELECTOR, /\[tabindex\]:not\(\[tabindex='-1'\]\)/);
});
