import { useEffect, useId, useRef } from "react";
import type { KeyboardEvent, MouseEvent, ReactNode, RefObject } from "react";
import { createPortal } from "react-dom";
import { Icon } from "./Icon";

/** What can take keyboard focus inside a dialog (disabled and hidden controls excluded at use). */
export const FOCUSABLE_SELECTOR = [
  "a[href]",
  "area[href]",
  "button:not([disabled])",
  "input:not([disabled]):not([type='hidden'])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "iframe",
  "summary",
  "[contenteditable='true']",
  "[tabindex]:not([tabindex='-1'])",
].join(", ");

/**
 * Where Tab must move focus to keep it inside the dialog, or null to let the browser move it.
 *   index: position of the focused element among the dialog's focusable elements (-1: none of
 *   them, e.g. the dialog itself) · count: how many there are · backwards: Shift+Tab.
 * Returns -1 when there is nothing focusable (focus stays on the dialog).
 */
export function trapTarget(index: number, count: number, backwards: boolean): number | null {
  if (count <= 0) return -1;
  if (index < 0 || index >= count) return backwards ? count - 1 : 0;
  if (!backwards && index === count - 1) return 0;
  if (backwards && index === 0) return count - 1;
  return null;
}

function focusables(root: HTMLElement | null): HTMLElement[] {
  if (!root) return [];
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(
    (el) => !el.hasAttribute("disabled") && el.getAttribute("aria-hidden") !== "true" && el.getClientRects().length > 0,
  );
}

/**
 * An accessible modal dialog: role="dialog" + aria-modal, labelled by its title (and
 * described by `description`), focus moves in when it opens and is kept inside (Tab and
 * Shift+Tab wrap), Escape closes it, and focus returns to the element that opened it.
 * The page behind does not scroll while it is open.
 *
 * Closing is never a decision: an approval shown in a modal stays pending when the modal
 * closes (Escape, the close button or a click outside call `onClose` only).
 */
export function Modal({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  size = "medium",
  initialFocusRef,
  closeLabel = "Close dialog",
  dismissible = true,
}: {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  description?: ReactNode;
  children?: ReactNode;
  /** Buttons, right-aligned at the bottom. */
  footer?: ReactNode;
  size?: "small" | "medium" | "large";
  /** What gets focus on open (default: the first focusable element in the body, else the dialog). */
  initialFocusRef?: RefObject<HTMLElement>;
  closeLabel?: string;
  /** false: no close button, Escape and outside clicks do nothing (the footer must offer a way out). */
  dismissible?: boolean;
}) {
  const titleId = useId();
  const descriptionId = useId();
  const dialogRef = useRef<HTMLDivElement>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  const returnFocus = useRef<HTMLElement | null>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    if (!open) return;
    returnFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const target = initialFocusRef?.current ?? focusables(bodyRef.current)[0] ?? dialogRef.current;
    target?.focus();
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = overflow;
      const back = returnFocus.current;
      if (back && back.isConnected) back.focus();
    };
    // initialFocusRef is read once, when the dialog opens.
  }, [open]);

  if (!open) return null;

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      if (dismissible) {
        event.stopPropagation();
        onCloseRef.current();
      }
      return;
    }
    if (event.key !== "Tab") return;
    const items = focusables(dialogRef.current);
    const active = document.activeElement;
    const next = trapTarget(items.findIndex((el) => el === active), items.length, event.shiftKey);
    if (next === null) return;
    event.preventDefault();
    (next === -1 ? dialogRef.current : items[next])?.focus();
  };

  const onBackdrop = (event: MouseEvent<HTMLDivElement>) => {
    if (dismissible && event.target === event.currentTarget) onCloseRef.current();
  };

  const dialog = (
    <div className="modal-backdrop" onMouseDown={onBackdrop}>
      <div
        ref={dialogRef}
        className={`modal modal-${size}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descriptionId : undefined}
        tabIndex={-1}
        onKeyDown={onKeyDown}
      >
        <div className="modal-head">
          <h2 id={titleId} className="modal-title">
            {title}
          </h2>
          {dismissible ? (
            <button type="button" className="icon-btn modal-close" aria-label={closeLabel} onClick={() => onCloseRef.current()}>
              <Icon name="x" size={16} />
            </button>
          ) : null}
        </div>
        {description ? (
          <p id={descriptionId} className="modal-description">
            {description}
          </p>
        ) : null}
        <div ref={bodyRef} className="modal-body">
          {children}
        </div>
        {footer ? <div className="modal-foot">{footer}</div> : null}
      </div>
    </div>
  );
  // In a browser the dialog renders at the end of <body>, above everything; on the server
  // (component tests) it renders in place.
  return typeof document === "undefined" ? dialog : createPortal(dialog, document.body);
}
