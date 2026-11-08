# protocols

The `system.toml` beside this file is the exact membership authority; this
README is narrative only.

## Responsibilities

- Typed validation of catalog and checkout documents.
- The checkout document is schema 9, and its two whole-document fields are part
  of the digest: `configuration_revision` is `null` or one lowercase
  64-character SHA-256 digest, and `folder_suggestions` holds entries of exactly
  `{path, package_count}` whose paths are absolute, strictly increasing in
  natural path order, never a configured folder, and whose counts are at least
  one package each; the list is empty while the package scan is unavailable.
- The canonical digest helper and the sort keys.
- The schema constants that `catalog` and `checkouts` import, and the field
  limits `catalog` imports.
- Imports nothing internal.

## Outside this boundary

- Producing either document (`catalog`, `checkouts`).

## Collaborators

- Used by `catalog`, `checkouts`, `lifecycle-control`, and
  `application-serving`.

## Cross-system invariants

- `derive_removable` is the single derivation of every Removable verdict and
  count of a checkout clone. `checkouts` calls it when it builds the document and
  `parse_checkouts` recomputes it from the document's own facts, so a document
  whose verdicts disagree with its facts is refused. No other module computes a
  blocker.
- `derive_attention` is the single derivation of every clone's availability,
  each repository's free-clone ranking and the attention list. `checkouts`
  calls it after the join and `parse_checkouts` recomputes all three from the
  document's own facts and refuses any inequality, so no consumer needs its own
  rules for what needs attention.
- `code_status_text` is the single text of a package's Code Status.
  `checkouts` calls it when it builds the document and `parse_checkouts`
  recomputes it from the stored fields, so a document whose text disagrees with
  its fields is refused. The same parser refuses a field set in a state that
  does not use it, a field missing from a state that does, and a `merged` status
  whose cleanup counts are both positive; it still requires a cleanup exactly for
  `merged`, so a merged branch with nothing left to clean has a cleanup of two
  zeros and reads plain `merged`, and no consumer needs its own rendering or
  consistency rules.
