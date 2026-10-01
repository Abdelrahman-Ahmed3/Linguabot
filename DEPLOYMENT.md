# Production and development

Both bots run the same Python code and can use the same Firebase project.
Set the environment on the host, alongside the Discord token and Firebase credentials.
No persistent local configuration file is needed on the host.

| Setting | Production | Development |
| --- | --- | --- |
| `BOT_ENV` | `prod` | `dev` |
| `DISCORD_TOKEN` | Existing bot used for production | Separate test bot token |
| `FIREBASE_CREDS` | Existing Firebase credentials | Same Firebase credentials |
| Settings document | `prod_config/settings` | `config/settings` |
| Learners collection | `prod_users` | `users` |
| Task claims collection | `prod_thread_submissions` | `thread_submissions` |

If `BOT_ENV` is absent, the bot selects development. Any other value causes a startup error.
The collections separate application data; both credentials still have access to the same database.

## One-time Firebase preparation

From a computer with the project dependencies and existing `FIREBASE_CREDS` available:

```sh
python setup_environments.py --dry-run
python setup_environments.py
```

The script fills missing development settings using the original committed test IDs,
preserves existing development channels and admins, and creates production settings
using the supplied production IDs. It does not modify users or task claims.
If production settings already exist, it leaves them alone.
Production user and task collections are created automatically when the bot first writes to them.
The script is run separately; do not add it to the host's normal startup command.

## Start production

1. Stop the old deployed version before starting the replacement with the same bot token.
2. Deploy the updated files and set `BOT_ENV=prod`. Keep the existing production bot token and Firebase credentials.
3. Start the bot using `python main.py`.
4. In a normal text channel in the actual server, run `.setserver` as an administrator.
5. Restart the bot so slash commands register for that server.
6. Run `/configure` and select the actual server's channels, task forum and two admins.
   This starts the background tasks once their required settings are available.
7. Run `/leaderboard` to create the production leaderboard message.

The production settings start with an empty leaderboard message ID, so the test
leaderboard is not reused. No point or streak reset is needed for the new production collections.

## Start development

Create a separate Discord bot, invite it to the test server, and enable the privileged
intents requested by the code (Message Content, Server Members and Presence).
Run the same code locally with its token, `BOT_ENV=dev`, and the existing Firebase credentials.
The existing test server settings and data remain available. If the bot uses a different
test server, update its server/channel settings before testing.

## Configuration and releases

Emojis, task tag IDs and winner role IDs now live in the selected Firebase settings document.
Restart after changing these values. `worksheet_points_emojis` is a five-item array
ordered 20, 22, 24, 26, 28; `winner_roles` is a three-item array ordered first, second, third.
`task_forum_ids` is a map with worksheet, reactivation, vocab, retell and connect entries.

Use `main` for production code and a development branch for changes. Merging code
does not copy settings between Firebase documents. Branches do not choose the environment:
the host's `BOT_ENV` setting does. Branch creation, pushing and host deployment are separate steps.
