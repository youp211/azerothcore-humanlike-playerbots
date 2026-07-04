# 11 — Arena team coordination (developer internals)

Deep, function-by-function reference for the custom arena-coordination code in
`mod-playerbots`. This is the debug/modify companion to the behaviour-level
write-up in [../BOT-BEHAVIOR.md](../BOT-BEHAVIOR.md) Section 7 ("Arena team
coordination"); terminology here matches that section but the explanation goes
down to the strategy engine, the named values, and one important gotcha in
`AiFactory`.

Everything below lives in the `mod-playerbots` static library that is linked
into `worldserver` (see [../BOT-ECONOMY.md](../BOT-ECONOMY.md) Section 5 for the
one-binary build model).

---

## 1. Purpose

Make a bot arena team fight like a coordinated team **without any inter-bot
messaging**: every member independently computes the *same* kill target
(healer-first, then bucketed-lowest-health) from shared world state, focuses it,
and holds offensive cooldowns until a shared "burst window" opens, at which point
the whole team unloads at once. If a real player is on the team, the bots defer
to whatever the human is attacking — the human calls targets simply by hitting
things. Coordination is emergent from determinism, not from a leader or a
network protocol.

## 2. Entry points & call graph

Execution enters through the bot's **strategy engine tick** (world thread). The
`arena` strategy is attached to both the combat and non-combat engines in
`AiFactory` when `player->InArena()` is true, and it registers three
trigger→action pairs in `ArenaStrategy::InitTriggers`. Each engine tick, the
engine evaluates the registered triggers (in priority order) and fires the
mapped action of the first that is active and useful.

```
Engine tick (world thread)
└─ ArenaStrategy::InitTriggers wires three TriggerNodes:
   ├─ "no possible targets" ─▶ "arena tactics"            (ACTION_BG)
   │     └─ ArenaTactics::Execute            (participation / reposition / move-to-center)
   ├─ "arena focus" ─────────▶ "attack arena kill target" (ACTION_HIGH + 5.0f)
   │     ├─ ArenaFocusTrigger::IsActive
   │     │     └─ AI_VALUE(Unit*, "arena kill target")  ─▶ ArenaKillTargetValue::Calculate
   │     │            ├─ [human override]  bg->GetPlayers() → member->GetVictim()
   │     │            └─ [deterministic]   guid-ordered scan, healer-first + bucketed HP
   │     └─ AttackArenaKillTargetAction::Execute  (inherited AttackAction::Execute)
   │            └─ GetTarget() → AI_VALUE(Unit*, "arena kill target") → Attack(target)
   └─ "arena burst window" ──▶ "arena burst sync"          (ACTION_HIGH + 6.0f)
         ├─ ArenaBurstWindowTrigger::IsActive
         │     ├─ HasBurstAura(member) over living same-team players
         │     ├─ else killTarget->GetHealthPct() <= 50.0f
         │     └─ returns (window != botAI->HasStrategy("boost", BOT_STATE_COMBAT))
         └─ ArenaBurstSyncAction::Execute
                └─ botAI->ChangeStrategy(bursting ? "-boost" : "+boost", BOT_STATE_COMBAT)
```

Registration points (name → factory):

| kind | registry (file) | name string | class |
|---|---|---|---|
| value | `ValueContext.h:262,515` | `"arena kill target"` | `ArenaKillTargetValue` |
| trigger | `TriggerContext.h:345` | `"arena focus"` | `ArenaFocusTrigger` |
| trigger | `TriggerContext.h:346` | `"arena burst window"` | `ArenaBurstWindowTrigger` |
| action | `ActionContext.h:126,363` | `"attack arena kill target"` | `AttackArenaKillTargetAction` |
| action | `WorldPacketActionContext.h:105,172` | `"arena tactics"` | `ArenaTactics` |
| action | `WorldPacketActionContext.h:106,173` | `"arena burst sync"` | `ArenaBurstSyncAction` |
| strategy | `StrategyContext.h:184` | `"arena"` | `ArenaStrategy` |

## 3. Function-by-function

### 3.1 `ArenaKillTargetValue::Calculate()`
`src/Ai/Base/Value/LeastHpTargetValue.cpp:38`

```cpp
Unit* ArenaKillTargetValue::Calculate()
```

Declared in `LeastHpTargetValue.h:25` as `class ArenaKillTargetValue : public
TargetValue`, ctor default name `"arena kill target"`. `TargetValue` derives
`UnitCalculatedValue` with `checkInterval = 1`, so the result is cached and
recomputed at most once per bot tick.

Steps:

1. **Guards.** Return `nullptr` unless `bot->InArena()`; fetch
   `Battleground* bg = bot->GetBattleground()` and return `nullptr` unless
   `bg && bg->GetStatus() == STATUS_IN_PROGRESS`.
2. **Human-victim override (runs first, wins).** Iterate `bg->GetPlayers()`.
   For each entry reacquire `Player* member = ObjectAccessor::FindPlayer(itr.first)`;
   skip if null or `member->GetBgTeamId() != bot->GetBgTeamId()`. Skip bots:
   `PlayerbotAI* memberAI = GET_PLAYERBOT_AI(member); if (memberAI && !memberAI->IsRealPlayer()) continue;`
   Read `Unit* victim = member->GetVictim()`; if `victim && victim->IsAlive() &&
   victim->IsPlayer() && victim->ToPlayer()->GetBgTeamId() != bot->GetBgTeamId()`,
   **return that victim immediately**. First qualifying human (in the map's
   guid order) wins.
3. **Deterministic pick (fallthrough).** `Unit* best = nullptr;
   uint32 bestScore = 0xFFFFFFFF;` Iterate `bg->GetPlayers()` again; reacquire
   `Player* enemy`; skip null / dead / same-team. Compute
   `uint32 score = (PlayerbotAI::IsHeal(enemy, true) ? 0 : 1000) + uint32(enemy->GetHealthPct()) / 15;`
   Keep the strictly-lowest score (`if (score < bestScore)`), return `best`.

**Non-obvious logic:**
- The `+0 / +1000` healer term dominates the 0–6 health-bucket term
  (`GetHealthPct()` is 0–100, `/15` → 0–6), so **any healer always outranks any
  non-healer** regardless of health.
- **Bucketing (`/15`)** collapses health into ~7 buckets so the pick doesn't
  flap between two similarly-hurt targets as their bars tick; the target only
  changes when someone crosses a 15%-wide boundary.
- **Determinism = agreement.** `bg->GetPlayers()` is a guid-ordered map and the
  scoring is a pure function of world state, so every teammate running this
  exact code converges on the same `best` with no communication. The strict
  `<` makes the guid-first candidate win ties.

**Inputs:** `bot`, live `Battleground`, its player map, per-player health / spec.
**Output:** a `Unit*` (an enemy `Player`) or `nullptr`. **Side effects:** none
(pure read; result cached by the value engine).

### 3.2 `ArenaFocusTrigger::IsActive()`
`src/Ai/Base/Trigger/PvpTriggers.cpp:340`; declared `PvpTriggers.h:29`
(`Trigger(botAI, "arena focus", 2)` → check every 2 ticks).

```cpp
bool ArenaFocusTrigger::IsActive()
{
    if (!bot->InArena())
        return false;
    Unit* killTarget = AI_VALUE(Unit*, "arena kill target");
    return killTarget && bot->GetVictim() != killTarget;
}
```

Active iff in arena, a kill target exists, and the bot isn't already hitting it.
Drives the `"attack arena kill target"` action at `ACTION_HIGH + 5.0f`.

### 3.3 `HasBurstAura(Player* player)` (file-local)
`src/Ai/Base/Trigger/PvpTriggers.cpp:351` — `static bool`.

Iterates a `static uint32 const burstAuras[]` of **18 iconic 3.3.5a offensive
cooldown aura IDs** and returns true on the first `player->HasAura(spellId)`:

| id | aura | id | aura |
|---|---|---|---|
| 1719 | Recklessness | 51713 | Shadow Dance |
| 12292 | Death Wish | 19574 | Bestial Wrath |
| 31884 | Avenging Wrath | 3045 | Rapid Fire |
| 12472 | Icy Veins | 2825 | Bloodlust |
| 12042 | Arcane Power | 32182 | Heroism |
| 11129 | Combustion | 16166 | Elemental Mastery |
| 13750 | Adrenaline Rush | 47241 | Metamorphosis |
| 51690 | Killing Spree | 49016 | Hysteria |
| — | — | 50334 | Berserk (feral) |
| — | — | 49206 | Summon Gargoyle |

The list is deliberately class-spanning and includes the raid-wide 2825/32182
(Bloodlust/Heroism), so one shaman popping lust opens the window for everyone.

### 3.4 `ArenaBurstWindowTrigger::IsActive()`
`src/Ai/Base/Trigger/PvpTriggers.cpp:380`; declared `PvpTriggers.h:40`
(`Trigger(botAI, "arena burst window", 2)`).

```cpp
bool ArenaBurstWindowTrigger::IsActive()
```

Steps:
1. Guard: `bot->InArena() && bot->IsAlive()`, and `bg` with
   `GetStatus() == STATUS_IN_PROGRESS`, else `false`.
2. `bool window = false;` Iterate `bg->GetPlayers()`; reacquire `member`; skip
   null / dead / not-same-team; if `HasBurstAura(member)` set `window = true`
   and `break`. (Teammate = bot **or** human; the human's wings count.)
3. If no aura window, use the execute-range fallback:
   `Unit* killTarget = AI_VALUE(Unit*, "arena kill target");
   window = killTarget && killTarget->GetHealthPct() <= 50.0f;`
4. **Return `window != botAI->HasStrategy("boost", BOT_STATE_COMBAT)`.**

Point 4 is the key: the trigger is active whenever the **desired** burst state
disagrees with the **current** `boost` strategy state. It therefore fires both
to turn boost *on* (window opened, boost currently off) and to turn it *off*
(window closed, boost currently on). The action is a pure toggle because the
trigger already encodes "they disagree".

### 3.5 `ArenaBurstSyncAction::Execute(Event)`
`src/Ai/Base/Actions/BattleGroundTactics.cpp:4434`; declared
`BattleGroundTactics.h:150` (`Action(botAI, "arena burst sync")`).

```cpp
bool ArenaBurstSyncAction::Execute(Event /*event*/)
{
    bool bursting = botAI->HasStrategy("boost", BOT_STATE_COMBAT);
    botAI->ChangeStrategy(bursting ? "-boost" : "+boost", BOT_STATE_COMBAT);
    LOG_DEBUG("playerbots", "[Arena] Bot {} {} burst window", bot->GetName(), bursting ? "leaves" : "enters");
    return true;
}
```

Flips the per-class `boost` strategy on the **combat** engine to reconcile with
the window (the trigger only fired *because* they disagreed). `boost` is the
stock playerbots strategy that fires offensive-cooldown actions, so toggling it
gates the entire class burst rotation. Emits the `[Arena] Bot X enters/leaves
burst window` debug line. Always returns `true`. **Side effect:** mutates the
bot's active strategy set.

### 3.6 `AttackArenaKillTargetAction`
`src/Ai/Base/Actions/ChooseTargetActions.h:94` /
`ChooseTargetActions.cpp:193`.

```cpp
class AttackArenaKillTargetAction : public AttackAction
{
    AttackArenaKillTargetAction(PlayerbotAI* botAI) : AttackAction(botAI, "attack arena kill target") {}
    std::string const GetTargetName() override { return "arena kill target"; }
    bool isUseful() override;
};
```

- **`Execute`** is inherited from `AttackAction::Execute` (`AttackAction.cpp:20`):
  `Unit* target = GetTarget();` → `Action::GetTarget()` →
  `context->GetValue<Unit*>(GetTargetName())->Get()` → i.e. resolves the very
  same `"arena kill target"` value from Section 3.1, null/`IsInWorld()`-checks it, then
  `Attack(target)`. So the action attacks exactly what `ArenaKillTargetValue`
  computed — no divergence between "who I focus" and "who I compute".
- **`isUseful()`** (`ChooseTargetActions.cpp:193`): false unless `bot->InArena()`;
  false if `botAI->ContainsStrategy(STRATEGY_TYPE_HEAL)` (healers don't drop
  healing to melee the kill target); then
  `Unit* killTarget = AI_VALUE(Unit*, "arena kill target"); return killTarget &&
  bot->GetVictim() != killTarget;`. This duplicates the `ArenaFocusTrigger`
  condition and adds the heal exclusion — so on a healer bot the trigger fires
  but the action is deemed not useful, and nothing happens.

### 3.7 `ArenaStrategy::InitTriggers`
`src/Ai/Base/Strategy/BattlegroundStrategy.cpp:83`; class
`BattlegroundStrategy.h:81` (`GetType()` → `STRATEGY_TYPE_GENERIC`,
`getName()` → `"arena"`).

```cpp
void ArenaStrategy::InitTriggers(std::vector<TriggerNode*>& triggers)
{
    triggers.push_back(new TriggerNode("no possible targets", { NextAction("arena tactics", ACTION_BG)}));
    triggers.push_back(new TriggerNode("arena focus", { NextAction("attack arena kill target", ACTION_HIGH + 5.0f)}));
    triggers.push_back(new TriggerNode("arena burst window", { NextAction("arena burst sync", ACTION_HIGH + 6.0f)}));
}
```

Relevances: `arena burst sync` (`ACTION_HIGH + 6.0f`) outranks `attack arena kill
target` (`ACTION_HIGH + 5.0f`), which outranks the `arena tactics` fallback
(`ACTION_BG`). Note the same `ArenaStrategy` instance is attached to *both*
engines (combat in `AiFactory.cpp:484`, non-combat in `AiFactory.cpp:710`),
but `ChangeStrategy(..., BOT_STATE_COMBAT)` in the burst-sync action targets the
combat engine specifically.

### 3.8 `ArenaTactics::Execute` (participation / dormancy fallback)
`src/Ai/Base/Actions/BattleGroundTactics.cpp:4250`; class
`BattleGroundTactics.h:136` (`MovementAction`). Fired by the stock `"no possible
targets"` trigger — i.e. when the bot has no combat target.

Relevant behaviour for this subsystem:
- **Leave-arena cleanup:** if `!bot->InBattleground()` it strips the `arena`
  strategy from both states (`ChangeStrategy("-arena", ...)`) and
  `ResetStrategies(!IsRandomBot)`, so the coordination strategy is torn down the
  moment the match ends.
- Handles `STATUS_WAIT_LEAVE` (→ `BGStatusAction::LeaveBG`), bails on non-
  `STATUS_IN_PROGRESS`, dead, moving, or `GetStartDelayTime() > 0` (the gates-
  closed startup phase — nothing bursts before the doors open).
- Drops `collision` and `buff` non-combat strategies, does a line-of-sight
  reposition toward the current victim, and otherwise `moveToCenter(bg)` when
  out of combat. This is the "stand in the middle and wait for a target" idle
  behaviour that keeps the bot participating between engagements.

## 4. Data structures & DB

- **No custom tables, no custom columns, no queries.** This subsystem is pure
  world-state computation. Contrast the gear-give / quest-help / sentiment
  systems, which read `mod_ollama_chat_*` tables (see
  [../BOT-BEHAVIOR.md](../BOT-BEHAVIOR.md) and
  [../BOT-ECONOMY.md](../BOT-ECONOMY.md)).
- **`static uint32 const burstAuras[]`** (Section 3.3) — the only module-level data,
  a fixed 18-entry spell-id table, file-local to `PvpTriggers.cpp`.
- **Named values** are the shared "data structure": `"arena kill target"` is a
  `UnitCalculatedValue` cached per bot per tick (`checkInterval = 1`). Triggers
  and the attack action all read it through `AI_VALUE(Unit*, ...)`, so there is
  one computation and one cached answer per bot per tick.
- Reads only live-object accessors: `bot->GetBattleground()`,
  `bg->GetPlayers()`, `bg->GetStatus()`, `Player::GetBgTeamId()`,
  `Player::GetVictim()`, `Unit::GetHealthPct()`, `Unit::HasAura()`,
  `PlayerbotAI::IsHeal(Player*, bool)`, `PlayerbotAI::IsRealPlayer()`.
- **Participation (upstream)** persists arena teams in `acore_characters`
  (`arena_team*` tables) via `RandomPlayerbotFactory::CreateRandomArenaTeams`;
  the coordination code itself writes nothing.

## 5. Concurrency & threading

Everything in this subsystem runs on the **world (map/update) thread** inside
the bot's `Engine::DoNextAction` tick. There are:
- **no detached worker threads** — unlike `mod-ollama-chat`, which fans LLM
  calls out to detached `std::thread`s (see [../BOT-ECONOMY.md](../BOT-ECONOMY.md)
  Section 4); arena coordination never touches Ollama, the DB, or the network, so it
  never leaves the world thread;
- **no mutexes** — the only shared reads are live game objects, read on the same
  thread that mutates them, within a single tick;
- **one cache** — the `"arena kill target"` value's `checkInterval = 1` cache,
  which is per-bot and world-thread-only.

**Why the "consensus without messaging" is safe:** within a tick the world
state (`bg->GetPlayers()`, healths, auras) is effectively immutable from each
bot's read-only perspective, and every bot runs the identical pure function over
the identical guid-ordered map, so they converge on the same target/window with
no locking and no race. Per-bot ticks are interleaved, not simultaneous, so even
transient disagreement (one bot ticked pre-damage, another post-damage) is
self-healing on the next tick and never corrupts shared state — there is no
shared mutable state to corrupt. All `Player*`/`Unit*` are reacquired from
`ObjectAccessor::FindPlayer` / `GetVictim` each call and used only within that
call, honouring the "never store a raw `Player*` across ticks" rule.

## 6. Config keys

The coordination logic (values / triggers / actions / strategy wiring) reads
**no `sConfigMgr` options** — the burst-aura list, the `/15` bucket width, the
`50.0f` execute threshold, and the healer weighting are all hardcoded by design.

The only configurable surface is upstream **participation**, read in
`PlayerbotAIConfig.cpp`:

| key | default | line |
|---|---|---|
| `AiPlayerbot.RandomBotArenaTeam2v2Count` | `10` | 727 |
| `AiPlayerbot.RandomBotArenaTeam3v3Count` | `10` | 728 |
| `AiPlayerbot.RandomBotArenaTeam5v5Count` | `5` | 729 |
| `AiPlayerbot.RandomBotArenaTeamMinRating` | `1000` | 732 |
| `AiPlayerbot.RandomBotArenaTeamMaxRating` | `2000` | 731 |
| `AiPlayerbot.DeleteRandomBotArenaTeams` | `false` | 730 |
| `AiPlayerbot.RandomBotAutoJoinBGRatedArena2v2Count` | `0` | 377 |
| `AiPlayerbot.RandomBotAutoJoinBGRatedArena3v3Count` | `0` | 379 |
| `AiPlayerbot.RandomBotAutoJoinBGRatedArena5v5Count` | `0` | 381 |

`CreateRandomArenaTeams` (`RandomPlayerbotFactory.cpp:786`) only accepts
captains with `player->GetLevel() >= 70` (guard at lines 817 and 843), which is
why the whole feature is **dormant on a fresh world until bots reach 70** — no
config toggles it on; it switches on by itself when level-70 captains exist.

## 7. Failure modes & gotchas

1. **The `boost`-in-arena contradiction (most important).** The arena branch of
   `AiFactory::AddDefaultCombatStrategies` (`AiFactory.cpp:482-488`) deliberately
   omits `boost` from its `addStrategiesNoInit(...)` list and even comments
   *"no boost here: burst cooldowns are held until the team burst window opens"*.
   **But four lines later, `AiFactory.cpp:495` unconditionally runs
   `engine->addStrategy("boost", false)` for the whole battleground block —
   arena included.** So an arena bot is actually created with `boost` **on**.
   The held-cooldown behaviour is therefore *not* enforced by the initial
   strategy set; it is enforced at runtime: on the first combat tick with no
   window, `ArenaBurstWindowTrigger::IsActive()` returns
   `false != HasStrategy("boost") == true` → fires → `ArenaBurstSyncAction`
   removes `boost`. Net steady state is correct ("held until window"), but if
   you are debugging "why did the bot blow a cooldown at the opening gates,"
   this ~1-window reconciliation lag (trigger `checkInterval = 2`) is the reason
   — usually masked because `GetStartDelayTime() > 0` and no enemy is in range
   during the opening seconds. The `addStrategy("boost", false)` `false` is the
   *init* flag, not an "off" flag; the strategy is enabled either way.
2. **Null reacquire-by-GUID.** Both the value and the burst trigger reacquire
   each player via `ObjectAccessor::FindPlayer(itr.first)` and skip on null, so a
   teammate/enemy who disconnected or was removed mid-match is silently ignored —
   no dangling pointer, matching the project's GUID-reacquire rule.
3. **Human-override precedence & multi-human ties.** The human-victim loop
   returns *before* the deterministic scan, so a human's target always wins. If
   two humans on one team attack different enemies, the first in the map's guid
   order wins (loop returns on first match) and all bots — running the same loop
   — assist that same one. If the human is attacking nothing valid (dead target,
   friendly, non-player), it falls through to the deterministic pick.
4. **Heal classification is spec-based.** `PlayerbotAI::IsHeal(enemy, true)`
   uses `bySpec = true`, so a healer in DPS spec (or an off-spec healer) may not
   score as a healer, and the team will target by health instead. A resto shaman
   parked in caster gear still counts (spec, not gear).
5. **Burst detection is aura-list-bound.** Cooldowns not in the 18-entry
   `burstAuras[]` (trinkets, Power Infusion 10060, engineering gadgets, most
   procs) do **not** open the window. The `killTarget <= 50%` execute clause is
   the catch-all that still gets the team to unload on a low target even when no
   listed aura is up. Adding a cooldown = add its spell id to the array.
6. **Healer bots: trigger fires, action no-ops.** `ArenaFocusTrigger` has no
   heal check, but `AttackArenaKillTargetAction::isUseful()` returns false under
   `STRATEGY_TYPE_HEAL`. So on healers the focus trigger fires each interval and
   the engine finds the action not useful — harmless churn, by design (healers
   shouldn't melee the kill target).
7. **No graceful-degradation machinery here — and that's expected.** Unlike the
   chat-adjacent subsystems, this code has no `information_schema` probe, no
   `[[gnu::weak]]` cross-module symbol, and no DB dependency, so there is nothing
   to degrade: it is always fully active whenever the `arena` strategy is
   attached. If you came here looking for a probe/weak-symbol fallback, there
   isn't one — the dependency is purely on core `Battleground`/`Player` APIs.
8. **Everything is inert outside arena.** Every entry point guards on
   `bot->InArena()` (and `STATUS_IN_PROGRESS`), so the value returns `nullptr`
   and the triggers return `false` in battlegrounds and the open world; the
   `arena` strategy is only attached while `player->InArena()`.

## 8. Cross-references

- [../BOT-BEHAVIOR.md](../BOT-BEHAVIOR.md) Section 7 — behaviour-level framing of arena
  coordination (the "what/why"; this doc is the "how"); Section 1 for the trigger /
  action / named-value strategy-engine model this subsystem plugs into.
- [../BOT-ECONOMY.md](../BOT-ECONOMY.md) Section 4–Section 5 — the async/detached-thread and
  `[[gnu::weak]]` patterns this subsystem deliberately does **not** use, and the
  one-binary static-link build that makes the strategy engine one process.
- Source of record: `src/Ai/Base/Value/LeastHpTargetValue.{h,cpp}`,
  `src/Ai/Base/Trigger/PvpTriggers.{h,cpp}`,
  `src/Ai/Base/Actions/ChooseTargetActions.{h,cpp}`,
  `src/Ai/Base/Actions/BattleGroundTactics.{h,cpp}`,
  `src/Ai/Base/Strategy/BattlegroundStrategy.{h,cpp}`,
  `src/Bot/Factory/AiFactory.cpp`, `src/PlayerbotAIConfig.cpp`,
  `src/Bot/Factory/RandomPlayerbotFactory.cpp`.
