# In-Game Test Checklist

Login: `admin` / `changeme123` (GM 3) via the desktop shortcut (self-heals the
realm address). Everything below works on the current clean world. Watch-side
logs while testing: `tail -f server/bin/Playerbots.log` and grep Server.log for
`[GearGive]`, `[QuestHelp]`, `[BotAH]`, `[GuildName]`, `[EventMail]`.

**Helper: find bots by personality**
```sql
SELECT c.name, c.level, t.playstyle FROM acore_characters.characters c
JOIN acore_characters.mod_ollama_chat_personality p ON p.guid = c.guid
JOIN acore_characters.mod_ollama_chat_personality_templates t ON t.`key` = p.personality
WHERE p.personality = 'WOW_MOM' AND c.online = 1;
```

**Helper: make a bot like you (for gated features)**
```sql
-- your guid: SELECT guid FROM acore_characters.characters WHERE name='YourChar';
UPDATE acore_characters.mod_ollama_chat_bot_player_sentiments
   SET sentiment_value = 0.9 WHERE player_guid = <you>;
-- or insert a row for a specific bot pair if none exists yet
```

## 1. Chat voice + personalities (v3 live now)
- [ ] `/say hi` near bots → terse lazy-caps 2008 replies in ~1-2 s
- [ ] Find an `EGIRL` and an `ELITE_ARENA_PVPER` (helper query) — voices should
      be unmistakable (uwu/cutie vs "what rating"/"stay 1200")
- [ ] Insult a bot, then compliment — tone tracks sentiment

## 2. Gear awareness
- [ ] On a fresh/low character with empty slots: bots reference your weak
      slot and correct class stat ("you need agility", "that belt is rough")
- [ ] As GM in epics (`.additem` a set): recognition, not nagging — raid-set /
      "solid for your level" respect lines

## 3. Gear gives (the big one)
- [ ] Chat repeatedly near a generous bot (WOW_MOM / CHILL_DAD / HUMBLE_FARMER,
      12-15% per gear-context, cooldowns apply). Eventually: an in-person offer
      ("open trade, got something for you")
- [ ] Open trade with that bot → it places the item, accepts → take it →
      LLM-generated completion whisper
- [ ] Seller variant (GOLD_FARMER/TRADER): it asks for silver in the window —
      pay → completes; don't pay → it won't accept
- [ ] Mail variant: join a group with the bot first (or share its guild), chat
      → "check your mailbox" → mailbox has the item (COD if seller)
- [ ] Force-test tip: `UPDATE mod_ollama_chat_personality_templates SET
      gear_give_chance = 50 WHERE `key`='WOW_MOM';` + console `ollama reload`,
      and clear cooldowns by restarting worldserver if needed
- [ ] Watch: `[GearGive]` in Server.log, `[GearGive] ... handed item over` in
      Playerbots.log

## 4. Quest-help invites
- [ ] Set sentiment ≥ 0.6 with a questing bot (helper above), pick up the
      quest it's on, quest within 30 yd → within ~10-25 min: say-line + group
      invite (2%/30 s check). `[QuestHelp]` in Playerbots.log
- [ ] No cold-invites from stranger bots (sentiment 0.5 < 0.6 threshold)

## 5. Guilds
- [ ] `/who` — themed guilds visible (Ascended Legion, Merciless Bloodbath,
      Cozy Adventurers...)
- [ ] Roster coherence:
```sql
SELECT g.name, p.personality, COUNT(*) FROM acore_characters.guild g
JOIN acore_characters.guild_member gm ON gm.guildid = g.guildid
JOIN acore_characters.mod_ollama_chat_personality p ON p.guid = gm.guid
GROUP BY g.name, p.personality ORDER BY g.name;
```
      Raid/pvp guilds: only fit personalities. Casual: mixed.
- [ ] (v4 + next reset) guild names come from the leader's personality via LLM
      — `[GuildName]` log lines

## 6. Bot memory (guilded players only)
- [ ] Join a bot guild as your character: `.guild invite` (GM) or ask me to
      add you via DB
- [ ] Duel a guildmate bot → then chat with it → it references the duel
- [ ] Run a BG/arena on a bot's team (needs level 10+ for WSG) → afterwards
      say "you suck" → it recalls playing with you
- [ ] Leave the guild → memories purged:
      `SELECT * FROM mod_ollama_chat_event_memories WHERE player_guid=<you>;`

## 7. Organic auction house
- [ ] Give bots ~30-60 min of town wandering, then check any auction house —
      listings owned by bot names, prices scattered (elitists high, fools low)
```sql
SELECT c.name AS seller, ah.buyoutprice, ii.itemEntry
FROM acore_characters.auctionhouse ah
JOIN acore_characters.characters c ON c.guid = ah.itemowner
JOIN acore_characters.item_instance ii ON ii.guid = ah.itemguid LIMIT 20;
```
- [ ] `[BotAH]` lines in Playerbots.log

## 8. Event mail (quest completion rewards)
- [ ] Group with a bot, complete a quest together → occasional thank-you mail
      (personality chance × 0.5; rare by design). Force: raise
      `OllamaChat.EventMailChanceMultiplier` in mod_ollama_chat.conf + restart.
      `[EventMail]` in Server.log

## 9. Pricing voices (sharper after v4)
- [ ] Ask "how much you want for that" — ELITE_ARENA_PVPER quotes absurd
      ("50g. resilience isnt cheap"), SCARED_NEWBIE undercharges apologetically,
      GOLD_FARMER quotes market

## 10. Ambient playstyles
- [ ] Inns: idlers sitting; towns: socializers loitering; mob camps: grinders.
      Spot-check a bot's expected behavior via the helper query's playstyle.

## Dormant until bots level
- Arena coordination (needs 70+; teams auto-form, rated queues enabled)
- AH buy-side (mod-ah-bot): needs one character created on the AHBOT account,
  then set `AuctionHouseBot.EnableBuyer = 1` in mod_ahbot.conf and restart.

## 11. Group-join + who-me (proximity only)
- [ ] Lone bot, `/say can you help me? party up?` → invite lands (or in-voice
      decline). `[GroupJoin]` logs show the rolled chance
- [ ] 2+ bots, ask without naming → staggered "who me?" in different voices;
      answer with a name or "yeah you" → that bot proceeds
- [ ] Channel messages must NOT produce invites from distant bots

## 12. Channels
- [ ] General is quiet-but-alive: mostly LFG/trade/recruitment lines from the
      personalities that would post them; generic chatter gets few bot replies
- [ ] Ask "anyone want to run Deadmines?" in General → relevant bots bite and
      the conversation references earlier channel lines
- [ ] `[Channel]` announces appear a few per hour with real players around

## 13. Guild lifecycle (emergent / recruiting / party→guild)
- [ ] Fresh world starts with ~2 guilds; more appear over time (one per ~5 min
      up to 12). `grep "\[EmergentGuild\]" server/bin/Playerbots.log`
- [ ] Guilds grow past their small founding size as founders recruit nearby
      bots. `grep "\[OngoingRecruit\]"` (gentle: ~1 player invite / 30 min max)
- [ ] Hang near a guild-founder bot that likes you → eventually a guild-invite
      popup with an in-voice pitch (not spammy)
- [ ] **Party→guild**: group with a few bots, run together 5+ min without
      wiping → chance it forms a guild (bots suggest names in-voice); OR say
      "let's guild up" in party chat to trigger it if the party qualifies.
      `grep "\[PartyGuild\]"`. A wipe (several deaths) makes it decline.
- [ ] `./validate-guilds.sh` → all PASS/WARN, no FAIL

## 14. PvP respect (appreciation / rescue / gank)
- [ ] **No "AI was reset" leak**: invite bots to a group, then leave/disband →
      *no* "AI was reset to defaults" bubbles in /say (the original bug)
- [ ] **Rescue**: let a friendly bot drop low fighting an enemy-faction bot,
      land the killing blow on its attacker → a grateful line (chatty
      personalities) and respect jump. `grep "\[PvpFriend\]" .../Playerbots.log`
      → "rescued". Verify with `.ollama sentiment view <bot> <you>`
- [ ] **Appreciation**: honorable kills beside fighting bots → small respect
      rise for the group; a talkative witness may say "good work"
- [ ] **Gank**: kill an enemy-faction bot 10+ levels below you, in view of a
      friendly bot, with no bigger fight around → a disapproving line + respect
      drop. Then confirm the exclusions: it should NOT count as a gank if a bot
      was already fighting that target, or a high-level enemy is nearby
- [ ] **Personality gate**: the quiet types stay silent (respect still moves);
      only chatty personalities pipe up, and only with a real player in earshot
- [ ] **Config**: `OllamaChat.Pvp*` knobs in `mod_ollama_chat.conf` tune ranges,
      respect deltas, and voice chances
