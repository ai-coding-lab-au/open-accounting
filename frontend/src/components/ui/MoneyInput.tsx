import { useState } from "react";
import {
  cleanMoney,
  groupThousands,
  blockScientificNotation,
} from "../../lib/numericInput";

/**
 * Money/quantity input that shows thousands separators when NOT focused
 * ("1,234.56") and the plain clean number while the user is editing
 * ("1234.56"). Editing the plain value means we never reformat mid-keystroke,
 * so there is no caret-jumping — the well-known failure mode of live-grouping
 * inputs. `value` is the clean string the form holds (no commas); `onChange`
 * receives the clean string.
 */
export default function MoneyInput({
  value,
  onChange,
  className,
  placeholder,
  ariaLabel,
}: {
  value: string;
  onChange: (clean: string) => void;
  className?: string;
  placeholder?: string;
  ariaLabel?: string;
}) {
  const [focused, setFocused] = useState(false);

  // Focused: show the raw clean value so editing never reflows the caret.
  // Blurred: show the grouped value for readability.
  const display = focused ? value : groupThousands(value);

  return (
    <input
      className={className}
      type="text"
      inputMode="decimal"
      placeholder={placeholder}
      aria-label={ariaLabel}
      value={display}
      onFocus={() => setFocused(true)}
      onBlur={() => setFocused(false)}
      onChange={(e) => onChange(cleanMoney(e.target.value))}
      onKeyDown={blockScientificNotation}
    />
  );
}
