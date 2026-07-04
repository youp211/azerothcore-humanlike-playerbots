#!/usr/bin/env bash
# Validate the personality-guild system against the live DB + logs.
# Prints PASS/WARN/FAIL per invariant. Read-only. Run anytime after guilds form.
#   ./validate-guilds.sh
set -uo pipefail
PLOG=/home/admin/git/wow/server/bin/Playerbots.log
SLOG=/home/admin/git/wow/server/bin/Server.log
q(){ sudo mariadb -N -e "$1" 2>/dev/null; }
pass(){ printf '  \033[32mPASS\033[0m %s\n' "$1"; }
warn(){ printf '  \033[33mWARN\033[0m %s\n' "$1"; }
fail(){ printf '  \033[31mFAIL\033[0m %s\n' "$1"; }

GUILDS=$(q "SELECT COUNT(*) FROM acore_characters.guild;")
echo "Personality-guild validation — $GUILDS guilds"

# 1. Formation & leaders: every guild has an online leader who is a member
BADLEAD=$(q "SELECT COUNT(*) FROM acore_characters.guild g
  LEFT JOIN acore_characters.characters c ON c.guid=g.leaderguid
  LEFT JOIN acore_characters.guild_member m ON m.guid=g.leaderguid AND m.guildid=g.guildid
  WHERE c.guid IS NULL OR c.online<>1 OR m.guid IS NULL;")
[ "$BADLEAD" = 0 ] && pass "every guild has an online, member leader" || fail "$BADLEAD guild(s) with missing/offline/non-member leader"

# 2. No bot in two guilds; no guild over the 15 cap
DUPE=$(q "SELECT COUNT(*) FROM (SELECT guid FROM acore_characters.guild_member GROUP BY guid HAVING COUNT(*)>1) x;")
OVER=$(q "SELECT COUNT(*) FROM (SELECT guildid FROM acore_characters.guild_member GROUP BY guildid HAVING COUNT(*)>15) x;")
{ [ "$DUPE" = 0 ] && [ "$OVER" = 0 ]; } && pass "membership integrity (no dupes, none over 15)" || fail "dupes=$DUPE over-cap=$OVER"

# 3. Leader personality fits its archetype (archetype from the log's Formed lines)
MISMATCH=0
while IFS='|' read -r arch leaderpers; do
  case "$arch" in
    raid)   ok="HARDCORE_RAIDLEAD THEORYCRAFTER MIN_MAXER LORE_NERD";;
    pvp)    ok="ELITE_ARENA_PVPER PVP_TRASHTALKER DUELIST RAGER";;
    casual) ok="WOW_MOM MENTOR GUILD_RECRUITER CHILL_DAD JOLLY_BEER_LOVER HEROIC_LEADER";;
    *) continue;;
  esac
  grep -qw "$leaderpers" <<<"$ok" || { MISMATCH=$((MISMATCH+1)); echo "      mismatch: $arch leader is $leaderpers"; }
done < <(grep -oE "Formed (raid|pvp|casual) guild '[^']*' led by [^ ]+ \([A-Z_]+\)" "$PLOG" | sed -E "s/Formed ([a-z]+) guild.*\(([A-Z_]+)\)/\1|\2/")
[ "$MISMATCH" = 0 ] && pass "all leaders fit their archetype (from log)" || fail "$MISMATCH leader/archetype mismatches"

# 4. Recruitment deployed one leader per guild
RECRUIT=$(grep -c "left to recruit" "$PLOG")
[ "$RECRUIT" = "$GUILDS" ] && pass "recruitment deployed ($RECRUIT/$GUILDS leaders)" || warn "recruit deploys=$RECRUIT vs guilds=$GUILDS (differ if a reset happened between)"

# 5. LLM naming coverage
RENAMED=$(grep -c "renamed guild" "$SLOG")
[ "$RENAMED" -ge "$GUILDS" ] && pass "LLM names cover all guilds ($RENAMED)" \
  || warn "$RENAMED/$GUILDS guilds got an LLM name; the rest kept the themed fallback (async rename dropped under load)"

# 6. Invites actually sent during the window (0 is expected if no real newbie was in range)
INV=$(grep -cE "GuildRecruit.*(invite|sent)" "$PLOG")
echo "  INFO $INV guild invite(s) sent this window (0 is normal if no unguilded sub-10 real player was near a recruiter)"

echo "Done. Deep dive: docs/internals/12-guilds.md"
