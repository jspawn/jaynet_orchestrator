---
name: files-docs
description: Create, edit, and organize files and documents — write and edit files, grep across trees, bundle or extract archives, and work with PDFs. Load when writing, editing, saving, or archiving files or documents.
---
# Files & Documents

**Trigger:** write, edit, save, create, archive, pdf, document, file, folder

## File tools
* `fs.write` — create or overwrite a file (text or binary from base64).
* `fs.edit` — surgical line-range replacement in an existing file.
* `fs.grep` — regex search across files (like ripgrep).
* `fs.list`, `fs.read`, `fs.find` — (always available in core)

## Archives
* `archives.create` — bundle files/folders into `.tar.gz`, `.zip`, etc.
* `archives.extract` — unpack an archive into a target directory.
* Load the **archives** skill when an archive is uploaded or needs inspecting (safe peeking via its bundled script, traversal-safety notes, formats).

## PDF
* `pdf.create` — generate a PDF from Markdown, HTML, or text content.

## Bulk documents
* `docs.summarize` — summarize or extract across a WHOLE tree of folders/files by processing each in its OWN sub-agent. ALWAYS prefer this over reading many files yourself (that blows the context window).

## Delivery
* `deliver.files` — (always available) hand produced files/folders back as a download. Bundles to `.tar.gz` if multiple.

## Tips
* Don't guess file paths — `fs.find` by name first.
* Use relative paths within the workspace.
* For multi-file edits, `code.patch` (unified diff) is often cleaner than many `fs.edit` calls.
