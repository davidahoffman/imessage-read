# imessage-read

A small macOS skill for agents that need to read local iMessage and SMS history.

Find a person's conversations, search ordinary text and `attributedBody`, and
retrieve available history with dates, sender/direction, message identifiers,
attachment metadata and explicit retrieval gaps. SQLite connections are read-only.

Useful for questions like: “What has my property manager asked for whose
completion is still unconfirmed?” The skill distinguishes an unanswered message
from unfinished work and cites the evidence.

## Install

Requires macOS with local Messages history, Python 3.9+ with SQLite, and Apple's
Swift toolchain with Foundation for attributed-body decoding. No third-party
Python packages, CRM or credentials are required.

For Codex, clone into your skills directory (choose a different destination if
that directory already exists):

```sh
git clone https://github.com/davidahoffman/imessage-read.git "${CODEX_HOME:-$HOME/.codex}/skills/imessage-read"
cd "${CODEX_HOME:-$HOME/.codex}/skills/imessage-read"
python3 scripts/reader.py check
```

If macOS denies access, manually allow the actual executing host app or terminal
under System Settings → Privacy & Security → Full Disk Access, relaunch it, and
rerun `check` in that environment. Toolchain installation is manual too. Optional
Contacts lookup may have separate access or schema limitations.

Retrieved excerpts enter the agent's model context. Local database access does
not mean retrieved text stays entirely on your Mac. The skill does not send
messages, alter Messages, or change system permissions.

## Use

Ask your agent:

> Use $imessage-read to review my property manager's requests. Resolve ambiguous
> identities, cite the messages, and distinguish unconfirmed completion from
> unfinished work. Consult no other records.

Or run the reader directly:

```sh
python3 scripts/reader.py find --query 'Alex Morgan' --contacts
python3 scripts/reader.py find --query '+15550100001'
python3 scripts/reader.py history --chat 42 --limit 200
python3 scripts/reader.py search --chat 42 --query 'invoice' --limit 200
```

Phone/email lookup works without Contacts. Confirm the intended person and all
relevant chats before retrieving history. Follow every page using the returned
watermark and cursor; search can have zero-match pages with more rows remaining.
See [SKILL.md](SKILL.md) for coverage and citation rules.

## Verification and limits

Tested on one Mac running macOS 26.5.2 and Python 3.14.2. Minimal live checks
confirmed read-only schema access, one attributed-body decode and one history
row; message text was not printed. Nine synthetic tests cover archives, Unicode,
text-field differences, search, paging, WAL visibility, identity ambiguity,
provenance, gaps and write rejection. An independent synthetic request audit also
checked how the skill reasons about completion evidence.

```sh
python3 tests/test_reader.py
```

Tests create temporary synthetic fixtures and never open live Messages or
Contacts. Archive tests require Swift and are skipped when it is absent.

Another Mac, older macOS/Python versions, a fresh permissions setup, live Contacts
compatibility, large histories and every archive format still need testing.
Foundation's legacy `NSUnarchiver` API is deprecated. Unsupported archives are
reported as gaps; decoding may need updates on future macOS versions.

“All locally available rows retrieved” does not mean a complete history.
Deleted, expired, unsynced, orphaned/unjoined messages, missing identities,
other channels and previous edit versions are not recovered. Attachment contents
and rich payloads are not reviewed. Concurrent edits/deletions can change the
view between pages. Missing completion evidence is not proof something wasn't
done.

## License

MIT. See [LICENSE](LICENSE).
