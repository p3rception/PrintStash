"use client";

import { useCallback, useId, type ReactNode } from "react";
import { Check } from "lucide-react";
import { Button } from "@/components/ui/button";
import { DropdownMenu } from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { uiText } from "@/lib/locale";

interface TitleEditorProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  trigger: ReactNode;
  label: string;
  fieldLabel: string;
  value: string;
  onValueChange: (value: string) => void;
  onSave: () => void;
  busy: boolean;
  /** Omit to let the field be saved empty. */
  requiredMessage?: string;
  placeholder?: string;
  align?: "start" | "end";
  maxLength?: number;
  saveDisabled?: boolean;
  children?: ReactNode;
}

/** One anchored single-field editor for Model and Collection headers, with no page reflow. */
export function TitleEditor({
  open,
  onOpenChange,
  trigger,
  label,
  fieldLabel,
  value,
  onValueChange,
  onSave,
  busy,
  requiredMessage,
  placeholder,
  align = "start",
  maxLength,
  saveDisabled = false,
  children,
}: TitleEditorProps) {
  const inputId = useId();
  const errorId = useId();
  const empty = !!requiredMessage && !value.trim();
  // The shared dropdown attaches its portal after the opening render.
  const focusInput = useCallback((input: HTMLInputElement | null) => {
    input?.focus();
    input?.select();
  }, []);

  return (
    <DropdownMenu
      open={open}
      onOpenChange={(next) => {
        if (!busy) onOpenChange(next);
      }}
      trigger={trigger}
      role="dialog"
      aria-label={label}
      portal
      align={align}
      className="min-w-0"
      contentClassName="w-96 rounded-md border border-border bg-popover p-3 text-popover-foreground shadow-lg"
    >
      <form
        className="space-y-3"
        onSubmit={(event) => {
          event.preventDefault();
          if (!empty && !busy && !saveDisabled) onSave();
        }}
      >
        <div className="space-y-1.5">
          <label htmlFor={inputId} className="text-xs font-medium">
            {fieldLabel}
          </label>
          <Input
            id={inputId}
            ref={focusInput}
            value={value}
            onChange={(event) => onValueChange(event.target.value)}
            disabled={busy}
            placeholder={placeholder}
            maxLength={maxLength}
            aria-invalid={empty}
            aria-describedby={empty ? errorId : undefined}
            className="h-9"
          />
          {empty && (
            <p id={errorId} role="alert" className="text-xs text-destructive">
              {requiredMessage}
            </p>
          )}
        </div>
        {children}
        <div className="flex items-center justify-end gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={busy}
            onClick={() => onOpenChange(false)}
          >
            {uiText("Cancel")}
          </Button>
          <Button type="submit" size="sm" loading={busy} disabled={empty || saveDisabled}>
            {!busy && <Check className="h-4 w-4" aria-hidden />}
            {busy ? uiText("Saving…") : uiText("Save")}
          </Button>
        </div>
      </form>
    </DropdownMenu>
  );
}
