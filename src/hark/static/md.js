// Minimal Markdown renderer that keeps track of which source lines each block came from,
// so any block can be clicked and edited in place.
(function () {
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

  function inline(s) {
    let out = esc(s);
    out = out.replace(/`([^`]+)`/g, "<code>$1</code>");
    out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    out = out.replace(/(^|[^*\w])\*([^*\n]+)\*(?!\w)/g, "$1<em>$2</em>");
    out = out.replace(/(^|[^\w])_([^_\n]+)_(?!\w)/g, "$1<em>$2</em>");
    out = out.replace(/~~([^~]+)~~/g, "<del>$1</del>");
    out = out.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
    return out;
  }

  const isTable = (l) => /^\s*\|.*\|\s*$/.test(l);
  const isSep = (l) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(l);
  const cells = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());

  // Returns [{start, end, kind, html, level?, checked?}], end exclusive.
  function blocks(md) {
    const lines = md.replace(/\r\n/g, "\n").split("\n");
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) { i++; continue; }
      let m;
      if ((m = line.match(/^(#{1,6})\s+(.*?)\s*#*\s*$/))) {
        const lv = Math.min(m[1].length, 4);
        out.push({ start: i, end: i + 1, kind: "h", level: lv, html: `<h${lv} class="blk">${inline(m[2])}</h${lv}>` });
        i++; continue;
      }
      if (isTable(line)) {
        let j = i;
        while (j < lines.length && isTable(lines[j])) j++;
        const rows = lines.slice(i, j);
        let head = null, body = rows;
        if (rows.length > 1 && isSep(rows[1])) { head = cells(rows[0]); body = rows.slice(2); }
        let h = '<table class="blk">';
        if (head) h += "<thead><tr>" + head.map((c) => `<th>${inline(c)}</th>`).join("") + "</tr></thead>";
        h += "<tbody>" + body.filter((r) => !isSep(r)).map((r) => "<tr>" + cells(r).map((c) => `<td>${inline(c)}</td>`).join("") + "</tr>").join("") + "</tbody></table>";
        out.push({ start: i, end: j, kind: "table", html: h });
        i = j; continue;
      }
      if ((m = line.match(/^(\s*)([-*+]|\d+[.)])\s+(\[[ xX]\]\s+)?(.*)$/))) {
        const depth = Math.min(6, Math.floor(m[1].replace(/\t/g, "    ").length / 2));
        const ordered = /\d/.test(m[2]);
        const task = !!m[3];
        const checked = task && /x/i.test(m[3]);
        const cls = ["li", "blk", ordered ? "ol" : "", task ? "task" : "", checked ? "done" : ""].filter(Boolean).join(" ");
        const box = task ? `<input type="checkbox" ${checked ? "checked" : ""} aria-label="Done">` : "";
        out.push({ start: i, end: i + 1, kind: "li", task, checked,
          html: `<div class="${cls}" style="--d:${depth}" data-n="${ordered ? esc(m[2]) : ""}">${box}${inline(m[4])}</div>` });
        i++; continue;
      }
      if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
        out.push({ start: i, end: i + 1, kind: "hr", html: '<hr class="blk">' });
        i++; continue;
      }
      if ((m = line.match(/^\s*>\s?(.*)$/))) {
        out.push({ start: i, end: i + 1, kind: "quote", html: `<blockquote class="blk">${inline(m[1])}</blockquote>` });
        i++; continue;
      }
      out.push({ start: i, end: i + 1, kind: "p", html: `<p class="blk">${inline(line.trim())}</p>` });
      i++;
    }
    return out;
  }

  // Plain text for pasting into WhatsApp, email and the like: *bold* headings, • bullets,
  // ☐/☑ checkboxes, links as "text (url)", and headings with nothing under them dropped.
  function shareText(md) {
    const lines = md.replace(/\r\n/g, "\n").split("\n");
    const level = (l) => { const m = l.match(/^(#{1,6})\s/); return m ? m[1].length : 0; };
    const empty = (i) => {
      for (let j = i + 1; j < lines.length; j++) {
        if (!lines[j].trim()) continue;
        const lv = level(lines[j]);
        return lv > 0 && lv <= level(lines[i]);
      }
      return true;
    };
    const inl = (t) => t
      .replace(/\*\*([^*]+)\*\*/g, "*$1*")
      .replace(/`([^`]+)`/g, "$1")
      .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, "$1 ($2)");
    const out = [];
    lines.forEach((l, i) => {
      const lv = level(l);
      if (lv) {
        if (lv > 1 && empty(i)) return;
        const t = l.replace(/^#+\s+/, "").replace(/\s*#*\s*$/, "").replace(/\*\*/g, "");
        if (out.length && out[out.length - 1] !== "") out.push("");
        out.push(lv <= 2 ? `*${t}*` : `_${t}_`);
        return;
      }
      if (isTable(l) && isSep(l)) return;
      const m = l.match(/^(\s*)[-*+]\s+(\[( |x|X)\]\s+)?(.*)$/);
      if (m) out.push(m[1] + (m[2] ? (m[3] === " " ? "☐ " : "☑ ") : "• ") + inl(m[4]));
      else out.push(inl(l));
    });
    return out.join("\n").replace(/\n{3,}/g, "\n\n").trim() + "\n";
  }

  window.MD = { blocks, inline, esc, shareText };
})();
