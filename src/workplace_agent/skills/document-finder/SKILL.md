---
name: document-finder
description: Find workplace documents across email attachments and OneDrive with exact source navigation and verified PDF page citations.
---

1. Clarify ambiguous topics or versions, but search both email and OneDrive by default.
2. Call discover_workplace_documents for candidate leads. A Copilot answer is not page evidence.
3. Use search_workiq_paths and get_workiq_schema to discover available resource operations.
   Resolve actual drive/item IDs using returned metadata. Resolve actual message/attachment IDs
   by searching email and listing attachments. Do not translate URLs into invented IDs.
4. Call fetch_workiq_metadata for authoritative file names, folder paths, webUrl, email subject,
   sender, receivedDateTime and webLink. Observe tenant policy; do not bypass denied paths.
5. Call inspect_document with the metadata resource path and a short literal phrase from the
   relevant content. This retrieves the PDF privately and returns page-local quoted evidence.
   For drive Office files the tool requests a PDF conversion. Email Office attachments cannot
   be converted by the attachment endpoint and must not be assigned invented page numbers.
6. If there are no matches, use a shorter supported phrase. If inaccessible, scanned, encrypted,
   unsupported or too large, give the verified location and say "page not verified".
7. Explain exactly how to navigate: OneDrive link > file > PDF viewer page; or Outlook link >
   message subject/sender/date > named attachment > PDF viewer page.
8. Distinguish the one-based PDF page from a printed page label. A converted file's citation
   refers to the PDF rendition, not the editable document's changing layout.
9. Include only concise relevant quotes. Never claim completeness when paging or permissions
   restrict the search. Do not modify documents, upload attachments, or persist document bytes.
