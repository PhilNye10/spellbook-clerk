# The Spellbook Clerk

Discord bot for MTG game nights. Version 13 adds a private `/spellbook` command hub, Commander game recording/statistics, and draft tournament leaderboards while preserving the existing RSVP, table assignment, bracket, MTG feed, and opt-in notification workflows.

## Command hub

Run `/spellbook` to open the ephemeral navigation hub. Its buttons organize the bot's features and show clickable slash-command references. Existing direct commands remain available from Discord's command picker.

## Main commands

| Command | Purpose |
|---|---|
| `/spellbook` | Open the private command hub |
| `/tables create` | Find a Sesh event, import RSVPs, and post the roster card |
| `/commander record` | Record a completed game from a saved Commander pod |
| `/commander leaderboard` | Rank players by Commander wins and win rate |
| `/commander player` | Show a player's Commander record and commander stats |
| `/commander commanders` | Rank commanders across recorded games |
| `/commander history` | Show recent Commander games and their game IDs |
| `/commander delete` | Organizer-only removal of an incorrectly recorded Commander game |
| `/draft leaderboard` | View lifetime standings or filter by calendar year |
| `/draft player` | Show an individual draft career |
| `/draft history` | Show previous tournaments and champions |
| `/notifications setup` | Organizer-only setup of the MTG News and MTG Deals opt-in panel |

## Commander statistics

1. Run `/tables create` and choose **Commander Pods** on the roster card.
2. After a game, run `/commander record`.
3. Choose the event, then the pod that completed a game.
4. Enter the commander used by each player. Partner commanders can be entered in one field, separated by `+` or `/`.
5. Select the winner. The game is saved and contributes to player and commander statistics.

Creating event rosters, generating Commander pods and Draft brackets, rerolling assignments, recording Draft match winners, and recording/deleting Commander results are organizer-only. Server administrators and members with Manage Server are always authorized; additional members can be authorized by assigning them a configured Discord role ID or listing their user ID in `permissions` in `config.yaml`. Each game stores the event, pod, players, commander names, winner, and recording user. A pod can record multiple games, including games where players switch decks. `/commander delete game_id` removes an incorrectly recorded game and its statistics; the game ID is shown by `/commander history`.

Player statistics include games, wins, losses, win rate, current win streak, and commander-by-commander records. Commander rankings are grouped by case-insensitive commander name. Because commander names are free text, use consistent names when entering games to avoid splitting the same commander into separate entries.

Discord modals support up to five text inputs. The default pod maximum is five players. If you configure larger pods, the Commander game-recording modal will decline those pods rather than record incomplete commander data.

## Organizer permissions

Only server administrators, members with **Manage Server**, and members assigned a configured organizer role can create event rosters, generate Commander pods or Draft brackets, reroll assignments, and record or delete game results. Regular members can still open `/spellbook`, view all Commander and Draft leaderboards/player stats/history, and use the other non-organizer features.

In `config.yaml`, add the ID of your Discord organizer role under `permissions.organizer_role_ids` (Developer Mode → right-click the role → Copy Role ID):

```yaml
permissions:
  organizer_role_ids:
    - 123456789012345678
  organizer_user_ids: []
```

You can list more than one role ID. `organizer_user_ids` is an optional way to authorize individual users explicitly. Existing server administrators remain authorized automatically.

## Draft leaderboard

Draft brackets continue to use the existing 1v1 single-elimination workflow. Match results are copied into the statistics tables whenever an organizer records a winner. When the final is recorded, the tournament champion and runner-up are saved automatically.

- Draft appearances count each tournament entered.
- Tournament wins and runner-up finishes are tracked separately.
- Match wins/losses and match win rate count played matches only.
- Byes advance players but do not count as played matches or match victories.
- `/draft leaderboard year:2027` filters to a calendar year; omitting the year shows lifetime standings.

Existing saved brackets are backfilled into the new draft statistics tables during database initialization. Historical records can only be reconstructed from bracket data that remains in the existing SQLite database.

## Data and upgrades

The bot uses SQLite at `/data/mtg_tables.db`. Version 13 adds Commander and Draft statistics tables through additive database initialization. It does not drop or replace existing events, attendees, assignments, brackets, webhook delivery records, or feed state. Preserve `.env`, `config.yaml`, and the `data/` directory during updates.

## Existing functionality

- Sesh event-message scanning and local RSVP cache; Sesh Pro remains optional.
- Commander pod assignments and 1v1 single-elimination draft brackets.
- Persistent roster and bracket controls across container restarts.
- Official MTG announcements and r/sealedmtgdeals feed publishing.
- Three-day inactivity cleanup for deal forum posts; new replies reset the inactivity timer.
- Independent `MTG News` and `MTG Deals` opt-in roles with self-service buttons.
- Docker host port `8484` mapped to container port `8080`.

See `install_ubuntu.md` for installation and update instructions.

## Updating from a private GitHub repository

See [GITHUB_SETUP.md](GITHUB_SETUP.md) to connect this installation to a private GitHub repository. Once configured, future updates are installed with `./update.sh` from the project directory.


## v14.1 feed notification behavior

MTG News and MTG Deals feed posts no longer mention or ping the `MTG News` or `MTG Deals` roles. Channel access remains controlled by the Discord channel permission settings. Feed messages explicitly disable all mentions.


### v14.2 feed preview polish

- Restores compact Discord embed thumbnails for feed items when the publisher exposes an image.
- Improves image discovery using Open Graph/Twitter metadata, article image tags, and Reddit RSS media/content images.
- Keeps News and Deals posts silent with all mentions disabled.
