(() => {
  const MAX_BYTES = 1024 * 1024;
  const tooLarge = text => text.length > MAX_BYTES || new TextEncoder().encode(text).length > MAX_BYTES;
  const marker = text => {
    let characters = 0;
    for (const character of text) characters++;
    return `[truncated ${characters} characters before transfer; exceeds 1 MiB]`;
  };
  // Omitting the whole item avoids cutting an uncleaned secret or path at the browser boundary.
  const text = input => {
    const value = String(input);
    return tooLarge(value) ? marker(value) : value;
  };
  const object = input => {
    const shown = JSON.stringify(input);
    return tooLarge(shown) ? {_qajev_source_truncated: marker(shown)} : input;
  };
  const value = input => {
    if (typeof input === 'string') return text(input);
    if (input === null || typeof input !== 'object') return input;
    const shown = JSON.stringify(input);
    if (!tooLarge(shown)) return input;
    const note = marker(shown);
    if (!Array.isArray(input) && typeof input.ok === 'boolean' && typeof input.says === 'string' &&
        input.evidence !== null && typeof input.evidence === 'object' && !Array.isArray(input.evidence)) {
      const kept = {ok: input.ok, says: text(input.says), evidence: object(input.evidence), _qajev_source_truncated: note};
      if (tooLarge(JSON.stringify(kept))) {
        kept.says = marker(input.says);
        kept.evidence = {_qajev_source_truncated: marker(JSON.stringify(input.evidence))};
      }
      return kept;
    }
    return Array.isArray(input) ? [note] : {_qajev_source_truncated: note};
  };
  const error = input => {
    const original = String(input), kept = text(original);
    // Keep the fixed error class used by Python to distinguish a broken check from a product failure.
    const kind = /^(?:Uncaught )?(TypeError|ReferenceError|SyntaxError)\b/.exec(original);
    return kept !== original && kind ? kind[1] + ': ' + kept : kept;
  };
  return {text, value, error};
})()
