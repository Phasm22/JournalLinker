<%*
// Pause to let the fast M4 file system settle
await new Promise(resolve => setTimeout(resolve, 100));

let dateStr = tp.date.now("YYYY-MM-DD");

// Rename only if started as "Untitled"
if (tp.file.title.startsWith("Untitled")) {
    try { await tp.file.rename(dateStr); } catch (e) {}
} else {
    dateStr = tp.file.title;
}

// Navigation tied to file title
let yesterday = moment(dateStr, "YYYY-MM-DD").subtract(1, "days").format("YYYY-MM-DD");
let tomorrow = moment(dateStr, "YYYY-MM-DD").add(1, "days").format("YYYY-MM-DD");
-%>
---
tags: [journal]
mood: ""
energy: ""
---

# Daily Log — <% dateStr %>

[[<% yesterday %>|← Yesterday]] | [[<% tomorrow %>|Tomorrow →]]

---

## Journal

<% tp.file.cursor() %>

---

> [!TIP] Memory Prompt
> What is one interaction from today that felt significant?
