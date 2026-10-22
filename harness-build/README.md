# harness-build (staging)

Staging copy of the two harness repositories, kept here until
`moopgit/harness-template` and `moopgit/harness-records-001` exist on GitHub.

- `RETURN.md`: the return package for grading (evidence, registry entries, notes).
- `harness-template.bundle`, `harness-records-001.bundle`: the full git history
  of each repo. Pushing from a bundle keeps the commit IDs listed in `RETURN.md`.

To publish once the empty private repos exist:

```
git clone -b main harness-template.bundle harness-template
git -C harness-template push https://github.com/moopgit/harness-template.git main
git clone -b main harness-records-001.bundle harness-records-001
git -C harness-records-001 push https://github.com/moopgit/harness-records-001.git main
```
