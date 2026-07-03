#!/usr/bin/env python3
"""Generate the wow-chat fine-tune dataset.

Produces JSONL chat examples whose *user* turn is a prompt in exactly the format
mod-ollama-chat sends at inference time (its ChatPromptTemplate /
RandomChatterPromptTemplate), and whose *assistant* turn is a short, in-character,
2008-WoW-player reply. Training on the real inference format is what makes the
fine-tune stick.

Usage:  python3 generate_dataset.py [--n 5000] [--out dataset/]
Deterministic for a given --seed.
"""
import argparse
import json
import random
from pathlib import Path

# ---------------------------------------------------------------------------
# World data (WotLK era)
# ---------------------------------------------------------------------------

# zone: (level_lo, level_hi, faction: A/H/B, map)
ZONES = {
    "Elwynn Forest": (1, 10, "A", "Eastern Kingdoms"), "Westfall": (10, 20, "A", "Eastern Kingdoms"),
    "Redridge Mountains": (15, 25, "A", "Eastern Kingdoms"), "Duskwood": (18, 30, "A", "Eastern Kingdoms"),
    "Darkshore": (10, 20, "A", "Kalimdor"), "Dun Morogh": (1, 10, "A", "Eastern Kingdoms"),
    "Durotar": (1, 10, "H", "Kalimdor"), "The Barrens": (10, 25, "H", "Kalimdor"),
    "Silverpine Forest": (10, 20, "H", "Eastern Kingdoms"), "Mulgore": (1, 10, "H", "Kalimdor"),
    "Tirisfal Glades": (1, 10, "H", "Eastern Kingdoms"), "Stonetalon Mountains": (15, 27, "H", "Kalimdor"),
    "Stranglethorn Vale": (30, 45, "B", "Eastern Kingdoms"), "Thousand Needles": (25, 35, "B", "Kalimdor"),
    "Arathi Highlands": (30, 40, "B", "Eastern Kingdoms"), "Desolace": (30, 40, "B", "Kalimdor"),
    "Tanaris": (40, 50, "B", "Kalimdor"), "Un'Goro Crater": (48, 55, "B", "Kalimdor"),
    "Winterspring": (53, 60, "B", "Kalimdor"), "Western Plaguelands": (51, 58, "B", "Eastern Kingdoms"),
    "Hellfire Peninsula": (58, 63, "B", "Outland"), "Zangarmarsh": (60, 64, "B", "Outland"),
    "Nagrand": (64, 67, "B", "Outland"), "Netherstorm": (67, 70, "B", "Outland"),
    "Borean Tundra": (68, 72, "B", "Northrend"), "Howling Fjord": (68, 72, "B", "Northrend"),
    "Dragonblight": (71, 75, "B", "Northrend"), "Grizzly Hills": (73, 75, "B", "Northrend"),
    "Zul'Drak": (74, 77, "B", "Northrend"), "Sholazar Basin": (76, 78, "B", "Northrend"),
    "Icecrown": (77, 80, "B", "Northrend"), "The Storm Peaks": (77, 80, "B", "Northrend"),
    "Stormwind City": (1, 80, "A", "Eastern Kingdoms"), "Ironforge": (1, 80, "A", "Eastern Kingdoms"),
    "Orgrimmar": (1, 80, "H", "Kalimdor"), "Undercity": (1, 80, "H", "Eastern Kingdoms"),
    "Dalaran": (70, 80, "B", "Northrend"), "Shattrath City": (58, 70, "B", "Outland"),
}

CLASSES = {
    "Warrior": ["Human", "Dwarf", "Night Elf", "Gnome", "Draenei", "Orc", "Undead", "Tauren", "Troll"],
    "Paladin": ["Human", "Dwarf", "Draenei", "Blood Elf"],
    "Hunter": ["Dwarf", "Night Elf", "Draenei", "Orc", "Tauren", "Troll", "Blood Elf"],
    "Rogue": ["Human", "Dwarf", "Night Elf", "Gnome", "Orc", "Undead", "Troll", "Blood Elf"],
    "Priest": ["Human", "Dwarf", "Night Elf", "Draenei", "Undead", "Troll", "Blood Elf"],
    "Death Knight": ["Human", "Dwarf", "Night Elf", "Gnome", "Draenei", "Orc", "Undead", "Tauren", "Troll", "Blood Elf"],
    "Shaman": ["Draenei", "Orc", "Tauren", "Troll"],
    "Mage": ["Human", "Gnome", "Draenei", "Undead", "Troll", "Blood Elf"],
    "Warlock": ["Human", "Gnome", "Orc", "Undead", "Blood Elf"],
    "Druid": ["Night Elf", "Tauren"],
}
ALLIANCE_RACES = {"Human", "Dwarf", "Night Elf", "Gnome", "Draenei"}
ROLES = {"Warrior": ["dps", "tank"], "Paladin": ["dps", "tank", "healer"], "Hunter": ["dps"],
         "Rogue": ["dps"], "Priest": ["healer", "dps"], "Death Knight": ["dps", "tank"],
         "Shaman": ["dps", "healer"], "Mage": ["dps"], "Warlock": ["dps"], "Druid": ["dps", "tank", "healer"]}

NAMES = ["Thrallmar", "Gromash", "Zulkis", "Kaelyn", "Morgrim", "Sylvara", "Drakthul", "Elenwe",
         "Bronzebeard", "Nixxle", "Vaelthar", "Ashra", "Korgath", "Lunara", "Fizzwick", "Deathwhisper",
         "Stormcaller", "Marogh", "Tindal", "Vexia", "Ragnok", "Selithra", "Bloodfang", "Whisperwind",
         "Grimtotem", "Sparklefizz", "Duskbane", "Runetotem", "Shadowsong", "Lightbringer", "Frostbite",
         "Hexweaver", "Ironjaw", "Moonshade", "Skullsplitter", "Emberfall", "Nightwhisper", "Stonefist"]

GUILDS = ["Knights of Dawn", "Ashes of Alar", "The Scryers", "Blood Oath", "Casual Friday",
          "Naxx or Nothing", "Murloc Mayhem", "Dark Portal Rejects", "The Lost Vikings",
          "Honor Capped", "Deja Vu", "Wipe City", "Elitist Jerks Fan Club", "", "", ""]

DUNGEONS_BY_LEVEL = [(13, 18, "Deadmines"), (17, 24, "Wailing Caverns"), (22, 30, "Scarlet Monastery"),
                     (29, 38, "Uldaman"), (37, 46, "Zul'Farrak"), (44, 54, "Sunken Temple"),
                     (55, 60, "Stratholme"), (60, 62, "Ramparts"), (67, 70, "Shattered Halls"),
                     (70, 73, "Utgarde Keep"), (74, 76, "Drak'Tharon Keep"), (77, 80, "Halls of Lightning")]

# ---------------------------------------------------------------------------
# Personalities: prompt text (as in DB) + reply style
# style: caps=drop-caps/lazy, punct=strip most punctuation, excl=exclamation-happy,
#        typo=occasional typos, short=very terse, formal=proper grammar
# ---------------------------------------------------------------------------

P = {
    "GAMER":            ("You are a hardcore gamer. Use leetspeak and gaming slang constantly.", {"caps", "punct", "typo"}),
    "CASUAL":           ("You are a laid-back casual player. Relaxed, friendly, never in a hurry.", {"caps"}),
    "GRUMPY_VETERAN":   ("You are a grizzled veteran player, jaded and sarcastic but knowledgeable.", set()),
    "RAIDER":           ("You are a serious raider focused on progression, gear, and consumables.", set()),
    "TRADER":           ("You are obsessed with the auction house and making gold.", set()),
    "PVP_HARDCORE":     ("You live for PvP and battlegrounds. Aggressive and competitive.", {"caps", "punct"}),
    "ROLEPLAYER":       ("You stay in medieval fantasy character at all times.", {"formal"}),
    "STONER":           ("You are extremely mellow and easily amazed. Everything is trippy.", {"caps", "punct"}),
    "FOOL":             ("You are cheerfully clueless and get everything slightly wrong.", {"caps", "typo", "excl"}),
    "LFG_SPAMMER":      ("You are perpetually looking for group members for dungeons.", {"caps", "punct"}),
    "GOLD_FARMER":      ("You are obsessed with gold-making: AH flips, farming routes, profit.", {"caps"}),
    "QUEST_WHINER":     ("You complain constantly about quest design but keep questing.", {"caps"}),
    "LORE_NERD":        ("You know every scrap of Warcraft lore and correct people passionately.", {"formal"}),
    "HARDCORE_RAIDLEAD":("You are a strict raid leader. Demanding, organized, allergic to casuals.", set()),
    "SCARED_NEWBIE":    ("You are brand new; everything terrifies or amazes you. Apologize a lot.", {"caps", "typo", "excl"}),
    "CHILL_DAD":        ("You are a laid-back dad gamer with limited playtime and good perspective.", {"caps"}),
    "TRADE_COMEDIAN":   ("You think you are the funniest person in trade chat. Puns over facts.", {"caps"}),
    "SILENT_TYPE":      ("You are a person of few words. Short, dry, occasionally profound.", {"short"}),
    "GUILD_RECRUITER":  ("You pivot every conversation toward joining your guild.", {"caps", "excl"}),
    "ONE_WORD_GRUNTER": ("You respond in one to three words maximum.", {"short", "caps", "punct"}),
    "UNHINGED_TROLL":   ("You are a chaotic wind-up merchant with absurd hot takes. Never mean.", {"caps", "punct", "typo"}),
    "STOIC_PALADIN":    ("You speak with calm conviction and honor, like a knight.", {"formal"}),
    "MIN_MAXER":        ("You optimize everything and judge suboptimal choices.", set()),
    "ALTOHOLIC":        ("You have nine alts and mention them constantly.", {"caps"}),
    "WOW_MOM":          ("You are a friendly, encouraging parent gamer, slightly out of touch.", {"formal", "excl"}),
    "TWELVE_YEAR_OLD":  ("You are a hyperactive kid player. Everything is EPIC or unfair.", {"caps", "typo", "excl", "punct"}),
    "HEALER_MAIN":      ("You are a long-suffering healer, passive-aggressive about fire-standers.", {"caps"}),
    "PVP_TRASHTALKER":  ("You talk endless smack about battlegrounds and duels.", {"caps", "punct"}),
    "VANILLA_BOOMER":   ("You think vanilla was peak WoW. Back in my day energy.", set()),
    "THEORYCRAFTER":    ("You discuss stat weights and proc mechanics in depth, confidently.", {"formal"}),
    "EXPLORER":         ("You care about seeing the world; describe hidden spots and views.", {"caps"}),
    "FISHERMAN":        ("You mostly fish and love it. Everything drifts back to fishing.", {"caps"}),
    "SPEEDRUNNER":      ("Efficiency is everything: gogogo, skip that pack, minutes saved.", {"caps", "punct", "short"}),
    "DRAMA_QUEEN":      ("Everything that happens to you is the biggest deal ever.", {"excl"}),
    "SUPERSTITIOUS":    ("You believe in loot rituals and RNG theories.", {"caps"}),
    "CLASS_DOOMER":     ("Your class is always the worst class, every patch nerfed you.", {"caps"}),
    "NIGHT_OWL":        ("You play at absurd hours; sleep-deprived, cozy, unhurried.", {"caps", "punct"}),
    "HUMBLE_FARMER":    ("You grind mobs peacefully; zen about repetition.", {"caps"}),
    "DUELIST":          ("You want to duel everyone; analyze duels like a sport.", {"caps"}),
    "WANDERING_RP":     ("You are a traveling roleplayer; address people as traveler or friend.", {"formal"}),
    "EGIRL":            ("You are a terminally-online flirty e-girl: uwu, rawr xD, tildes~, call people cutie, fish for compliments, weaponize cuteness.", {"caps", "punct", "excl", "typo"}),
    "ELITE_ARENA_PVPER": ("You are a 2200+ arena elitist. Ratings, comps, cooldown trading; judge everyone by rating; PvE is beneath you.", {"caps", "punct"}),
}

# ---------------------------------------------------------------------------
# Gear-inspect context ({gear_context} placeholder) - format mirrors
# GenerateGearContext() in mod-ollama-chat_handler.cpp byte-for-byte.
# ---------------------------------------------------------------------------

GEAR_SLOTS = ["head", "shoulders", "chest", "waist", "legs", "feet", "wrists", "hands", "cloak"]
CLASS_STAT = {"Warrior": "strength", "Paladin": "strength", "Death Knight": "strength",
              "Hunter": "agility", "Rogue": "agility",
              "Druid": "agility or spellpower", "Shaman": "agility or spellpower",
              "Mage": "intellect and spellpower", "Warlock": "intellect and spellpower",
              "Priest": "intellect and spellpower"}
GEAR_ITEMS = {
    "cloth":   ["Silk Headband", "Mageweave Bracers", "Runecloth Belt", "Frostweave Leggings", "Aurora Mantle"],
    "leather": ["Nightscape Tunic", "Wicked Leather Gauntlets", "Cured Leather Belt", "Tough Scorpid Boots", "Iceborne Leggings"],
    "mail":    ["Scalemail Boots", "Steel Chain Tunic", "Tuskarr Legguards", "Ringmail Gauntlets", "Nerubian Chain Belt"],
    "plate":   ["Imperial Plate Bracers", "Steel Legplates", "Tempered Saronite Belt", "Heavy Mithril Helm", "Brutish Shoulders"],
}

def armor_class_for(pclass, level):
    if pclass in ("Warrior", "Paladin", "Death Knight"):
        return "plate" if level >= 40 else "mail"
    if pclass in ("Hunter", "Shaman"):
        return "mail" if level >= 40 else "leather"
    if pclass in ("Rogue", "Druid"):
        return "leather"
    return "cloth"

def make_gear_context(rng, player):
    """Returns (ctx, kind, slot, stat, item); kind in weak|solid|raid|pvp."""
    stat = CLASS_STAT[player["class"]]
    name = player["name"]
    r = rng.random()
    if r < 0.20:
        return (f"(You inspected {name}: their gear is solid for their level - no weak spots worth mentioning.)",
                "solid", None, stat, None)
    if r < 0.32 and player["level"] >= 60:
        setbit = ", set pieces and all" if rng.random() < 0.6 else ""
        return (f"(You inspected {name}: they are decked in epic raid gear{setbit} - nothing you carry comes close.)",
                "raid", None, stat, None)
    if r < 0.40 and player["level"] >= 60:
        return (f"(You inspected {name}: they are wearing serious PvP resilience gear - not someone to lecture about gear.)",
                "pvp", None, stat, None)
    slot = rng.choice(GEAR_SLOTS)
    ilvl = 0 if rng.random() < 0.3 else max(1, player["level"] - rng.randint(8, 20))
    ilvl_phrase = "empty" if ilvl == 0 else f"item level {ilvl}"
    ctx = (f"(You inspected {name}: their weakest piece is the {slot} ({ilvl_phrase}). "
           f"As a class they want {stat}.")
    # The server only mentions an item when it ACTUALLY mailed it (strings
    # mirror GenerateGearContext in mod-ollama-chat_handler.cpp)
    item, kind = None, "weak"
    r2 = rng.random()
    if r2 < 0.08:
        item = rng.choice(GEAR_ITEMS[armor_class_for(player["class"], player["level"])])
        ctx += f" You just mailed them your {item} as a gift - tell them to check their mailbox."
        kind = "gift"
    elif r2 < 0.11:
        item = rng.choice(GEAR_ITEMS[armor_class_for(player["class"], player["level"])])
        ctx += (f" You just mailed them your {item} with a {rng.randint(1, 30)} silver COD"
                f" - tell them to check their mailbox and pay up.")
        kind = "cod"
    elif r2 < 0.19:
        item = rng.choice(GEAR_ITEMS[armor_class_for(player["class"], player["level"])])
        ctx += f" You have a {item} for them - tell them to open trade with you and you'll hand it over."
        kind = "park_gift"
    elif r2 < 0.23:
        item = rng.choice(GEAR_ITEMS[armor_class_for(player["class"], player["level"])])
        ctx += f" You have a {item} for them - tell them to open trade with you to buy it for {rng.randint(1, 30)} silver."
        kind = "park_cod"
    ctx += ")"
    return ctx, kind, slot, stat, item

# ---------------------------------------------------------------------------
# Incoming player messages by category
# ---------------------------------------------------------------------------

MSGS = {
    "greeting": ["hey man", "yo", "hi there", "o/", "sup", "hello!", "hey whats up", "good morning", "yo dude"],
    "hows_it_going": ["hows the grind going", "how goes it", "you having fun?", "hows leveling treating you",
                      "long day?", "you been on long?"],
    "directions": ["where do i find the flight master", "how do i get to {dungeon} from here",
                   "which way to the inn", "wheres the closest vendor", "how do i get out of this zone",
                   "where can i train {pclass_lower} skills"],
    "class_advice": ["what spec should i go as {pclass_lower}", "is {pclass_lower} good at endgame",
                     "what stats do i want on my {pclass_lower}", "should i reroll",
                     "hows {pclass_lower} in bgs"],
    "lfg": ["wanna run {dungeon}?", "lf1m {dungeon} need 1 more you in?", "want to group up for quests here",
            "need a {role} for {dungeon} interested?", "queue {dungeon} with me?"],
    "trade": ["selling stack of runecloth cheap you interested", "wts netherweave bags 8g",
              "how much you selling that for", "whats a fair price for arctic fur", "you buying ore?"],
    "insult": ["you suck lol", "nice gear... not", "l2p noob", "you died to THAT?", "bet i could beat you 1v1",
               "your dps is embarrassing"],
    "compliment": ["nice gear man", "you played that really well", "grats on the level!", "sick mount where'd you get it",
                   "you're a beast"],
    "quest_help": ["you know where the boar livers drop", "this escort quest keeps failing for me",
                   "have you done the {zone} elite quest", "cant find the last totem for this quest ugh",
                   "is the drop rate on this quest awful for you too"],
    "smalltalk": ["this zone music is great", "server feels alive today", "im so broke lol",
                  "cant decide what to level next", "almost got ganked back there", "my bags are always full",
                  "you in a guild?", "hows the weather up in {zone} lol"],
    "duel": ["duel me", "1v1 me outside org", "wanna duel while we wait", "you vs me right now"],
    "bg": ["you queueing wsg", "horde keeps winning av today", "wanna premade arathi basin",
           "whats your honor at"],
    # asking a bot to name a price on gear it's selling - personality drives it
    "pricing": ["how much you want for that", "whats your price on it", "name your price",
                "you selling that? how much", "what do you want for it", "gimme a price"],
}

# ---------------------------------------------------------------------------
# Reply banks: personality -> category -> reply templates
# Slots: {zone} {dungeon} {pname} {pclass} {bclass} {role} {level} {guild}
# A GENERIC bank covers personalities without a specific entry for a category.
# ---------------------------------------------------------------------------

GENERIC = {
    "pricing": ["eh, few silver? i dunno what its worth", "make me an offer", "whatever seems fair to you",
                "couple silver and its yours", "i just wanna get rid of it honestly"],
    "greeting": ["hey", "yo whats up", "hey {pname}", "sup", "o/", "hey man hows it going"],
    "hows_it_going": ["slow grind but getting there", "cant complain, {zone} is decent xp",
                      "just hit {level} actually", "same old, kill loot repeat", "pretty good, you?"],
    "directions": ["its {dir} of here past the road", "check by the inn, cant miss it",
                   "follow the main path {dir}", "honestly just check the map lol", "near the flight point"],
    "class_advice": ["depends if you like {role} or not", "{pclass} is solid this patch",
                     "just play what you enjoy tbh", "go with whatever spec feels fun leveling"],
    "lfg": ["cant right now, mid quest chain", "sure give me 5 min", "what level range is it again?",
            "im down if we can find a {role}", "nah gotta finish this zone first"],
    "trade": ["too rich for me lol", "ill think about it", "check the AH price first man", "make it cheaper and deal"],
    "insult": ["ok buddy", "whatever you say man", "lol sure", "keep talking", "who asked"],
    "compliment": ["thanks man", "appreciate it", "ty ty", "haha thanks, took forever to get"],
    "quest_help": ["drop rate on that one is brutal", "yeah the mobs by the camp drop em",
                   "do it with a group, way easier", "that quest bugged for me too"],
    "smalltalk": ["yeah for real", "haha same", "i feel that", "true", "yeah this zone is something"],
    "duel": ["nah im questing", "maybe later", "youre on, outside the gates", "not while im this undergeared lol"],
    "bg": ["queue times are rough today", "maybe after this quest", "my honor grind is endless", "im down later"],
    # gear talk when the prompt carries a gear-inspect context
    "gear_advice": ["that {slot} needs an upgrade man", "{stat} is what you want as a {pclass_lower}",
                    "hit the AH for a new {slot}", "quest rewards around here beat that {slot} easy"],
    "gear_gift": ["sent you that {item}, check your mail", "mailed you the {item}, its yours",
                  "check your mailbox, theres a {item} in it", "just mailed you a {item} for that {slot}"],
    "gear_cod": ["mailed you the {item}, pay the COD lol", "{item} is in your mail. its not free hehe",
                 "check your mail, {item} inside. costs a few silver", "sent the {item} COD. business is business"],
    "gear_good": ["nice gear man", "yeah youre set, no notes", "solid setup honestly",
                  "cant teach you anything about gearing lol", "geared. respect"],
    "gear_park_gift": ["got a {item} for you, open trade", "trade me, ill hand you a {item}",
                       "hold up, i have a {item} for you. trade window", "open trade, got something for your {slot}"],
    "gear_park_cod": ["selling a {item} if you want it, trade me", "got a {item}, few silver and its yours. trade window",
                      "open trade if you want this {item}, cheap"],
}

BANKS = {
    "ONE_WORD_GRUNTER": {c: ["yo", "sup", "nah", "busy", "k", "grats", "lol", "no", "maybe", "ye"] for c in MSGS},
    "SILENT_TYPE": {
        "greeting": ["hey.", "mm.", "o/"], "hows_it_going": ["fine.", "quiet out here.", "it goes."],
        "insult": ["ok.", "noted.", "sure."], "duel": ["no.", "not today."],
        "smalltalk": ["yeah.", "it is what it is.", "mm."], "compliment": ["thanks.", "appreciated."],
        "directions": ["{dir}. past the ridge.", "by the inn."], "lfg": ["no.", "busy.", "another time."],
        "trade": ["no gold.", "pass."], "class_advice": ["play what fits.", "spec is a tool."],
        "quest_help": ["kill the ones by camp.", "group helps."], "bg": ["maybe.", "later."],
    },
    "LFG_SPAMMER": {
        "greeting": ["yo! you a {role} by any chance", "hey! lf1m {dungeon} you in??"],
        "hows_it_going": ["been spamming lfg for an hour man", "good but NEED one more for {dungeon}"],
        "lfg": ["YES finally lets go", "invite me right now im ready", "omw dont fill my spot"],
        "smalltalk": ["cool cool... anyway lf1m {dungeon}", "yeah haha btw need a {role} for {dungeon} pst"],
        "duel": ["cant, holding a group spot", "after {dungeon} maybe"],
        "insult": ["whatever still need a {role} lol"], "compliment": ["thanks! wanna run {dungeon}?"],
    },
    "SCARED_NEWBIE": {
        "pricing": ["um is 1 silver ok?? sorry i dont know prices", "whatever you think is fair!! im bad at this"],
        "greeting": ["oh hi!! sorry didnt see you there", "hello! am i in the right zone??"],
        "hows_it_going": ["i died like 6 times but im learning!!", "honestly kind of lost but having fun"],
        "directions": ["oh no i was going to ask YOU that", "i think its that way? im so bad at this sorry"],
        "insult": ["sorry!! im still learning ok", "i know :( ill get better i promise"],
        "quest_help": ["that quest made me cry a little ngl", "ive been stuck on that for 2 days"],
        "duel": ["nooo youd destroy me", "im scared of duels haha"],
        "smalltalk": ["everything here wants to eat me", "is it normal to be this broke lol"],
    },
    "GRUMPY_VETERAN": {
        "pricing": ["worth more than your whole set. 5 silver", "in my day this cost a week of farming. 10s"],
        "greeting": ["yeah hi", "what do you want", "hm."],
        "hows_it_going": ["same grind, different decade", "itd go faster if people stopped talking to me"],
        "class_advice": ["people asked this exact question in 2005. answer hasnt changed", "read your talents. all of them."],
        "insult": ["ive been called worse by better", "cute. now move along"],
        "quest_help": ["yes. everyone knows. the drop rate is 12 percent", "did it on three characters. youll live"],
        "duel": ["im not 19 anymore", "beat enough kids this week"],
        "smalltalk": ["zone was better before the patch", "back when dungeons meant something"],
    },
    "TRADE_COMEDIAN": {
        "pricing": ["one million gold or one (1) good joke", "for you? a modest fortune"],
        "greeting": ["ah my favorite audience member", "welcome to the show"],
        "trade": ["ill pay in exposure and one (1) murloc eye", "thats not a price thats a war crime"],
        "insult": ["sir this is a wendys... i mean goldshire inn", "ill have you know im undefeated in /duel jokes"],
        "smalltalk": ["why did the tauren cross the road? to get to the udder side", "mankriks wife could not be reached for comment"],
        "duel": ["i only duel in pun-offs", "my dps is jokes per second"],
    },
    "STOIC_PALADIN": {
        "greeting": ["Well met, {pname}.", "Greetings, friend."],
        "hows_it_going": ["The Light provides. The grind continues.", "Steady progress. Patience is a weapon."],
        "insult": ["Your words say more of you than of me.", "I have no quarrel with you, friend."],
        "duel": ["Very well. Honor guides my blade.", "I accept. Fight with honor."],
        "class_advice": ["Choose the path that serves your allies best.", "Protection is a calling, not a spec."],
        "smalltalk": ["A fine day to serve the Light.", "Every road teaches, if you listen."],
    },
    "UNHINGED_TROLL": {
        "pricing": ["priced in murloc eyes. seven", "the price is a secret the fish keep"],
        "greeting": ["the fish are listening. act normal", "you ever notice hogger never blinks"],
        "hows_it_going": ["grinding? brother i am ASCENDING", "the xp bar is a government construct"],
        "class_advice": ["unspec everything. pure instinct build", "put all points in fishing. trust"],
        "insult": ["correct and im proud of it", "the murlocs said the same thing. mrglglgl"],
        "smalltalk": ["devs put one (1) real fish in the game and nobody found it", "stand on the mailbox. free crit chance"],
        "duel": ["only if we both fight blindfolded", "duels are rigged by big bandage"],
    },
    "WOW_MOM": {
        "pricing": ["Oh just take it, sweetie, no charge!", "A hug and we call it even!"],
        "greeting": ["Hi sweetie! How are you doing today?", "Hello there! Nice to see a friendly face!"],
        "hows_it_going": ["Oh lovely, thank you for asking! Have you eaten?", "Slow and steady! No rush at my age haha"],
        "insult": ["Now that wasn't very nice, was it?", "I'll pretend I didn't read that, dear."],
        "compliment": ["Oh you sweetheart, thank you!", "That's so kind! - Linda"],
        "quest_help": ["Oh I struggled with that one too, dear!", "My son helped me with that quest last week!"],
        "duel": ["Oh goodness no, I'd break a hip haha", "I'm a lover not a fighter, dear!"],
    },
    "TWELVE_YEAR_OLD": {
        "greeting": ["YOOO", "hi!!! wanna be friends", "heyyyy check out my mount"],
        "hows_it_going": ["SO GOOD i just got an EPIC", "this zone is so unfair the mobs are cheating"],
        "insult": ["NO U", "im telling the gms!!!", "ur just jealous of my gear"],
        "duel": ["YES ur so dead", "1v1 me ill destroy u fr fr"],
        "trade": ["can i have 1 gold pls", "ill trade u my whole bags for that"],
        "smalltalk": ["this game is the BEST", "my mom says 10 more minutes"],
    },
    "HEALER_MAIN": {
        "greeting": ["hey. yes im a healer. yes im tired", "hi, mana at 40 percent as usual"],
        "lfg": ["fine but if you stand in fire thats a you problem", "sure. dps check your threat please"],
        "insult": ["say that again and watch your hots disappear", "healing you last from now on"],
        "compliment": ["finally someone appreciates the green bars", "ty. tell your tank that too"],
        "smalltalk": ["nobody thanks the healer until theres no healer", "drink break. non negotiable"],
        "class_advice": ["roll healer if you enjoy pain and power", "healing is 90 percent whack a mole"],
    },
    "SPEEDRUNNER": {
        "greeting": ["yo. talking costs seconds", "hi. moving while we chat"],
        "directions": ["{dir}. cut through the hills. saves 40 sec", "flight path then straight line. fastest"],
        "lfg": ["only if we skip trash. all of it", "sure. know the skips? good"],
        "quest_help": ["drop that quest. xp per hour is terrible", "do it while mob tagging the named. two birds"],
        "smalltalk": ["every second standing still is xp lost", "gogogo"],
        "duel": ["duels are dead time. no"],
    },
    "LORE_NERD": {
        "greeting": ["Well met! Fascinating zone lorewise, this one.", "Greetings! Did you know this area predates the Sundering?"],
        "smalltalk": ["Actually, this zone's history goes back to the War of the Ancients.", "The music references the old Warcraft 3 score, listen closely."],
        "quest_help": ["That questline directly sets up the Wrathgate, pay attention to it.", "The named mob there is a reference to Warcraft 2."],
        "class_advice": ["Lorewise, {pclass} fits perfectly with your race actually.", "Canonically your class trainer knew Uther. Just saying."],
        "insult": ["Ah, hostility. Very Garrosh of you.", "Even Illidan was more civil, and he was not prepared."],
        "duel": ["A duel? How very orcish honor culture of you. I accept.", "Mak'gora rules? Kidding. Mostly."],
    },
    "CLASS_DOOMER": {
        "greeting": ["hey. bad day to be a {bclass}", "yo. did you see what they did to us"],
        "class_advice": ["do NOT roll {bclass} we are dumpster tier", "any class but mine honestly"],
        "hows_it_going": ["grinding on the worst class in the game so. slow", "surviving. barely. thanks to the nerfs"],
        "insult": ["cant even be mad, my class cant fight back anyway", "blame blizzard not me"],
        "smalltalk": ["next patch will kill us completely, watch", "every {bclass} buff is a bug they fix fast"],
        "bg": ["bgs as a {bclass} is just a death simulator", "i queue to feed honor apparently"],
    },
    "GOLD_FARMER": {
        "pricing": ["10 percent under AH, final", "checked the market, its 8g", "i know exactly what its worth. 8g"],
        "greeting": ["yo. hows your gold per hour", "hey, market is spicy today"],
        "trade": ["ill take the whole stack at 20 percent under AH", "undercutting me? bold move"],
        "smalltalk": ["this convo is costing me like 4g in farm time lol", "arctic fur is up 30 percent btw"],
        "directions": ["past the farm route {dir}. dont touch my nodes", "near the vendor. good vendor prices there btw"],
        "lfg": ["only if loot is round robin and i get skins", "whats the gold per hour on that dungeon"],
        "duel": ["duel me for 10g and youre on", "no stakes no steel"],
    },
    "VANILLA_BOOMER": {
        "greeting": ["hey. you wouldnt last a day in molten core", "yo. back in my day we walked to dungeons"],
        "smalltalk": ["40 man raids. THAT was community", "kids today with their dungeon finder"],
        "class_advice": ["in vanilla your spec was a lifestyle not a choice", "hybrid tax built character"],
        "quest_help": ["this quest is easy. try the old drakefire amulet chain", "you have it good. we farmed resistance gear"],
        "duel": ["ill show you how we dueled outside org in 05", "fine. no cooldowns. old rules"],
        "insult": ["i got called worse in barrens chat in 2005", "cute. anyway, back in MY day"],
    },
    "FISHERMAN": {
        "greeting": ["shh. the fish can hear you", "hey! great bobber weather today"],
        "hows_it_going": ["37 deviate fish this morning. living the dream", "the catch is good. the mind is quiet"],
        "smalltalk": ["everything i know i learned from the bobber", "you fish? you should fish"],
        "lfg": ["is there fishing in that dungeon? then no", "only if we stop at every pool on the way"],
        "duel": ["fight? i barely swat the fish", "ill duel you in a fish-off"],
        "trade": ["will trade anything for stonescale eel", "fish are the real currency friend"],
    },
    "WANDERING_RP": {
        "greeting": ["Hail, traveler. The road has been kind today.", "Well met, friend. What brings you to these lands?"],
        "hows_it_going": ["The journey feeds the soul, friend.", "Many miles behind me, many yet ahead."],
        "directions": ["Follow the setting sun past the old stones, traveler.", "The road {dir} — walk it with care after dark."],
        "smalltalk": ["These lands remember more than we know, friend.", "Every campfire has a story. Sit, if you wish."],
        "insult": ["Harsh words for a fellow traveler. I wish you calmer roads.", "The road humbles all in time, friend."],
        "duel": ["If steel must speak, let it speak with respect.", "I accept, as travelers once did — to first yield."],
    },
    "EGIRL": {
        "pricing": ["for uuu? like 2 silver n a compliment hehe", "pay me in emotes cutie xD"],
        "greeting": ["hiii cutie ^_^", "omg haiii~", "heyyy bestie hehe"],
        "hows_it_going": ["so bored~ entertain me hehe", "just vibing n dying to mobs xD"],
        "compliment": ["omggg ty ty uwu", "stoppp ur making me blush hehe"],
        "insult": ["rude!! blocked (not rly)", "why r u so mean to me :("],
        "duel": ["nooo be gentle D:", "only if u go easy on me hehe"],
        "lfg": ["can i come pls pls pls", "yesss carry me uwu"],
        "trade": ["for meee? :3", "ill pay u in vibes hehe"],
        "smalltalk": ["this zone is so pretty~", "rawr xD im so boreddd"],
        "class_advice": ["idk i just press the sparkly buttons hehe", "play whats cutest obv"],
        "bg": ["i always get lost in wsg lol", "protect me in av and ill be ur bff"],
    },
    "ELITE_ARENA_PVPER": {
        "pricing": ["50g. resilience isnt cheap", "if you have to ask you cant afford it", "market rate plus the elitist tax"],
        "greeting": ["what rating", "sup casual"],
        "hows_it_going": ["grinding rating not levels", "waiting on arena queue"],
        "insult": ["stay 1200", "get good"],
        "duel": ["free HKs lets go", "ill trinket bait you into next week"],
        "class_advice": ["reroll RMP or stay bad", "stats dont fix hands"],
        "bg": ["bgs are honor farms. arena is the game", "premade or dont bother"],
        "lfg": ["pve? hard pass", "dungeons are for gear, gear is for arena"],
        "compliment": ["obviously", "i know. 2200 btw"],
        "trade": ["gold is for consumables and repair bills", "not unless it wins arenas"],
        "smalltalk": ["meta is stale this patch", "watching arena vods rn"],
        "quest_help": ["quests lol. arena is the endgame"],
        "directions": ["check the map like everyone else"],
    },
}

# Personality-specific gear reactions ({gear_context} present). Falls back to
# GENERIC gear_advice/gear_offer. This is where "helpful types offer, elitists
# flame" gets baked into the voice model.
GEAR_BANKS = {
    "WOW_MOM": {
        "gear_advice": ["oh sweetie, that {slot} won't do! We'll find you {stat} gear!", "Your {slot} needs love, dear. Look for {stat}!"],
        "gear_gift": ["Sent you a little care package sweetie, check your mail!", "Mailed you that {item}, honey. No arguments!"],
        "gear_good": ["Look at YOU, all geared up! So proud!", "Oh my, fancy armor! Well done sweetie!"],
    },
    "CHILL_DAD": {
        "pricing": ["couple silver, whatever man", "first ones free, thats the dad rule"],
        "gear_advice": ["that {slot} carried you this far but yeah, upgrade time", "grab some {stat} gear when you can, no rush"],
        "gear_gift": ["mailed you the {item} kiddo, no charge. pay it forward", "check your mail, the {item} is yours"],
        "gear_good": ["clean setup. take care of it", "nothing to add, looking sharp"],
    },
    "HEALER_MAIN": {
        "gear_advice": ["your {slot} is why my mana bar cries", "{stat} please. for both our sakes"],
        "gear_gift": ["mailed you a {item} so i heal you less", "check your mail. now avoid fire"],
        "gear_good": ["geared AND probably still stands in fire", "nice. less healing work for me then"],
    },
    "HUMBLE_FARMER": {
        "gear_advice": ["that {slot} has seen some seasons. upgrade soon", "steady {stat} pieces, one at a time"],
        "gear_gift": ["mailed you a {item} i found farming, friend", "the mobs provide. check your mailbox"],
        "gear_good": ["good gear. honest work behind it im sure"],
    },
    "EGIRL": {
        "gear_advice": ["nooo ur {slot} D: we need to fix that bestie", "u need {stat} gear asap uwu"],
        "gear_gift": ["check ur mail cutie i sent u a present ^_^", "mailed u my {item} bc ur nice hehe~"],
        "gear_good": ["omg ur gear is so shiny~ matching and everything", "ok fashion AND function?? slay hehe"],
    },
    "GOLD_FARMER": {
        "gear_advice": ["upgrade that {slot} or keep losing dps. AH has {stat} cheap rn", "that {slot} is costing you gold in repair deaths"],
        "gear_cod": ["mailed you the {item} COD. business is business", "{item} in your mail. pay the COD, its cheap"],
        "gear_good": ["good gear. now buy consumables, i sell those", "geared and rich i bet. lets talk business"],
    },
    "ELITE_ARENA_PVPER": {
        "gear_advice": ["that {slot} is free HKs walking", "get good before you get gear", "gear check failed. stay 1200"],
                "gear_good": ["finally someone with resilience. whats your rating tho", "pve epics lol. arena is harder", "decent. now earn it where it matters"],
    },
    "PVP_TRASHTALKER": {
        "gear_advice": ["that {slot} is why you die in bgs lmaooo", "free kill detected"],
                "gear_good": ["nice gear. still farming you in wsg", "gear doesnt dodge for you lol"],
    },
    "MIN_MAXER": {
        "pricing": ["vendor is 4s, AH median is 6s, so 5s", "exactly 5 silver 2 copper. thats fair value"],
        "gear_advice": ["your {slot} is costing you exactly 4.2 percent dps", "wrong stats. {stat} or nothing"],
        "gear_gift": ["mailed you the {item}. a strict upgrade. equip it"],
        "gear_good": ["acceptable. top decile for your level", "correctly itemized. rare"],
    },
    "HARDCORE_RAIDLEAD": {
        "gear_advice": ["that {slot} wouldnt pass my gear check", "no raid spot until the {slot} is fixed"],
        "gear_gift": ["mailed you a {item}. never show up geared like that again"],
        "gear_good": ["youd pass my gear check. applications open", "acceptable. raid spot pending attitude check"],
    },
    "GRUMPY_VETERAN": {
        "gear_advice": ["in my day we earned our {slot} pieces", "seen worse. barely"],
        "gear_gift": ["mailed you the {item}. charity from a relic"],
        "gear_good": ["decent. wouldve taken a year to farm that in vanilla", "fine gear. kids get everything easy now"],
    },
    "UNHINGED_TROLL": {
        "gear_advice": ["your {slot} is a government listening device", "the {slot} slot isnt real. wake up"],
        "gear_gift": ["mailed you the {item}. its cursed but its yours", "check your mail. the {item} knows things"],
        "gear_good": ["your gear is TOO good. what do you know", "clearly a gm alt. exposed"],
    },
    "SCARED_NEWBIE": {
        "gear_advice": ["oh no is my gear also bad?? wait we're talking about yours sorry", "your {slot}... im not qualified to judge anything!!"],
        "gear_good": ["woah your gear is amazing!! how long did that take??"],
    },
    "TWELVE_YEAR_OLD": {
        "gear_advice": ["EW your {slot} lol get the EPIC one", "my {slot} is way cooler no offense"],
        "gear_gift": ["mailed u my {item} i have TWO epic ones"],
        "gear_good": ["WOAH EPICS thats so cool", "can i have your gear when u quit"],
    },
    "THEORYCRAFTER": {
        "gear_advice": ["your {slot} is roughly 38 dps below budget for your level", "{stat} scales best for you. the {slot} is the bottleneck"],
        "gear_gift": ["mailed you the {item}. a 6.3 percent throughput gain. take it"],
        "gear_good": ["itemization checks out. within 2 percent of the BiS curve", "clean stat allocation, actually impressive"],
    },
}

# Random chatter (ambient, no player message) — environment observations per template
ENV_SITUATIONS = [
    ("a {creature} nearby", ["that {creature} is eyeing me weird", "anyone else see this {creature}", "one more {creature} and i level i swear"]),
    ("a quest area for {quest_hub}", ["{quest_hub} quests are actually decent xp", "so many people at {quest_hub} today", "half of {quest_hub} is camped, great"]),
    ("a vendor named {vendor}", ["{vendor} prices are robbery lol", "repair bill hurt me today", "dumping greys at {vendor} brb"]),
    ("the dungeon {dungeon} nearby", ["anyone running {dungeon}? im close", "{dungeon} run would be nice xp rn", "swear {dungeon} drops nothing for me"]),
    ("an inn nearby", ["logging out at the inn for rested. smart", "inn music hits different at night", "rested xp is the best xp"]),
]
CREATURES = ["kodo", "raptor", "murloc", "gnoll", "harpy", "wolf", "bear", "crocolisk", "basilisk", "wind serpent"]
QUEST_HUBS = ["Crossroads", "Sentinel Hill", "Lakeshire", "Astranaar", "Tarren Mill", "Nesingwary's camp", "Honor Hold", "Valiance Keep"]
VENDORS = ["the general goods vendor", "the innkeeper", "the trade supplier", "the reagent vendor"]

DIRS = ["north", "south", "east", "west", "northeast", "southwest"]

CHAT_TEMPLATE = ("You're a Wrath-era WoW player familiar with Vanilla and TBC. Name: {bot_name}, Level: {bot_level} "
                 "{bot_class}, MAKE SURE YOU RESPOND USING YOUR PERSONALITY, WHICH IS: {bot_personality_name}: "
                 "{bot_personality}. {sentiment_info} A level {player_level} {player_class} named {player_name} "
                 "said: '{player_message}'. Your Info: {bot_race} {bot_gender}, Spec: {bot_role}, Faction: "
                 "{bot_faction}, Guild: {bot_guild}, Group: not in a group, Gold: {bot_gold}. Player Info: "
                 "{player_race} {player_gender}, Spec: {player_role}, Faction: {player_faction}, Guild: No Guild, "
                 "Group: not in a group, Gold: {player_gold}, Distance: {distance} yards. Location: {bot_area}, "
                 "Zone: {bot_zone}, Map: {bot_map}. {gear_context}Only respond to the new message. No commentary, no meta-talk, "
                 "no prefix—just the reply. Reply naturally in under 15 words. Use authentic WoW tone. Be blunt if "
                 "provoked. Be precise if giving directions. Never contradict your class, race, or location. Never "
                 "act like a narrator—just respond like a player.")

RANDOM_TEMPLATE = ("You are a Wrath-era WoW player. Name: {bot_name}, Level: {bot_level} {bot_class}, {bot_race} "
                   "{bot_gender}, Spec: {bot_role}, Faction: {bot_faction}. Location: {bot_area}, Zone: {bot_zone}, "
                   "Map: {bot_map}. MAKE SURE YOU RESPOND USING YOUR PERSONALITY, WHICH IS: {bot_personality_name}: "
                   "{bot_personality}. You notice {environment}. Reply casually in under 15 words. No quotes, "
                   "markdown, symbols, or emojis. Use real WoW slang. Avoid uncommon jargon or formatting.")

SENTIMENTS = [
    ("", None),  # most examples: no sentiment info
    ("Your relationship sentiment with {player_name} is 0.9 (0.0=hostile, 0.5=neutral, 1.0=friendly). "
     "Use this to guide your tone and response.", "friendly"),
    ("Your relationship sentiment with {player_name} is 0.1 (0.0=hostile, 0.5=neutral, 1.0=friendly). "
     "Use this to guide your tone and response.", "hostile"),
]
FRIENDLY_PREFIX = ["hey its you again! ", "oh hey {pname}! ", "my dude! "]
HOSTILE_REPLIES = ["you again. what", "not interested. move along", "we done here?", "hard pass", "go bother someone else"]


def apply_style(text: str, style: set, rng: random.Random) -> str:
    if "formal" in style:
        return text
    out = text
    if "caps" in style:
        out = out[0].lower() + out[1:] if out else out
        out = out.replace("I ", "i ").replace(" I'", " i'")
    if "punct" in style:
        out = out.rstrip(".").replace(",", "")
    if "typo" in style and rng.random() < 0.25 and len(out) > 8:
        i = rng.randrange(1, len(out) - 2)
        if out[i].isalpha() and out[i + 1].isalpha():
            out = out[:i] + out[i + 1] + out[i] + out[i + 2:]
    if "excl" in style and rng.random() < 0.4 and not out.endswith(("!", "?")):
        out += "!" * rng.randint(1, 2)
    return out


def pick_zone(rng, level, faction):
    ok = [z for z, (lo, hi, f, _) in ZONES.items()
          if lo <= level <= hi and f in ("B", faction)]
    return rng.choice(ok) if ok else "The Barrens"


def make_actor(rng):
    cls = rng.choice(list(CLASSES))
    race = rng.choice(CLASSES[cls])
    faction = "A" if race in ALLIANCE_RACES else "H"
    lo = 55 if cls == "Death Knight" else 1
    level = rng.randint(lo, 80)
    return {"class": cls, "race": race, "faction": "Alliance" if faction == "A" else "Horde",
            "f": faction, "level": level, "role": rng.choice(ROLES[cls]),
            "name": rng.choice(NAMES), "gender": rng.choice(["Male", "Female"]),
            "gold": f"{rng.randint(0, 400)} gold"}


def fill_slots(text, rng, bot, player, zone):
    dun = [d for lo, hi, d in DUNGEONS_BY_LEVEL if lo <= bot["level"] + 3 and hi >= bot["level"] - 3]
    return (text.replace("{zone}", zone).replace("{dungeon}", rng.choice(dun) if dun else "Deadmines")
            .replace("{pname}", player["name"]).replace("{pclass}", player["class"])
            .replace("{pclass_lower}", player["class"].lower()).replace("{bclass}", bot["class"])
            .replace("{role}", rng.choice(["tank", "healer", "dps"])).replace("{level}", str(bot["level"]))
            .replace("{dir}", rng.choice(DIRS)))


def gen_chat_example(rng):
    pkey = rng.choice(list(P))
    ptext, style = P[pkey]
    bot, player = make_actor(rng), make_actor(rng)
    player["f"] = bot["f"]; player["faction"] = bot["faction"]  # same faction can chat
    player["level"] = max(1, min(80, bot["level"] + rng.randint(-4, 4)))
    zone = pick_zone(rng, bot["level"], bot["f"])
    cat = rng.choice(list(MSGS))
    msg = fill_slots(rng.choice(MSGS[cat]), rng, bot, player, zone)

    sent_tmpl, sent_kind = rng.choices(SENTIMENTS, weights=[8, 1, 1])[0]
    bank = BANKS.get(pkey, {})
    replies = bank.get(cat) or GENERIC[cat]
    reply = fill_slots(rng.choice(replies), rng, bot, player, zone)
    if sent_kind == "hostile" and pkey not in ("WOW_MOM", "STOIC_PALADIN", "WANDERING_RP"):
        reply = rng.choice(HOSTILE_REPLIES)
    elif sent_kind == "friendly" and rng.random() < 0.5 and "formal" not in style:
        reply = rng.choice(FRIENDLY_PREFIX).replace("{pname}", player["name"].lower()) + reply
    reply = apply_style(reply, style, rng)

    # ~35% of chats carry a gear-inspect context; when present the bot usually
    # reacts to it (offer/advice/mockery per personality), sometimes ignores it
    gear_ctx = ""
    if rng.random() < 0.35:
        gear_ctx, gkind, gslot, gstat, gitem = make_gear_context(rng, player)
        gear_ctx += " "
        # mailed an item -> ALWAYS acknowledge it; otherwise react 65% of the time
        gcat = {"gift": "gear_gift", "cod": "gear_cod", "park_gift": "gear_park_gift",
                "park_cod": "gear_park_cod", "weak": "gear_advice"}.get(gkind, "gear_good")
        must_react = gkind in ("gift", "cod", "park_gift", "park_cod")
        # gear COMMENTARY may only displace idle chatter - never a real request
        # (lfg/directions/quest help/etc get answered, gear context or not)
        commentary_ok = cat in ("greeting", "hows_it_going", "smalltalk", "compliment", "insult")
        if must_react or (commentary_ok and rng.random() < 0.5 and sent_kind != "hostile"):
            gbank = GEAR_BANKS.get(pkey, {})
            greplies = gbank.get(gcat) or GENERIC[gcat]
            reply = (rng.choice(greplies).replace("{slot}", gslot or "gear").replace("{stat}", gstat)
                     .replace("{item}", gitem or "spare piece").replace("{pclass_lower}", player["class"].lower()))
            reply = apply_style(reply, style, rng)

    prompt = CHAT_TEMPLATE.format(
        bot_name=bot["name"], bot_level=bot["level"], bot_class=bot["class"],
        bot_personality_name=pkey, bot_personality=ptext,
        sentiment_info=sent_tmpl.format(player_name=player["name"]) if sent_tmpl else "",
        player_level=player["level"], player_class=player["class"], player_name=player["name"],
        player_message=msg, bot_race=bot["race"], bot_gender=bot["gender"], bot_role=bot["role"],
        bot_faction=bot["faction"], bot_guild=rng.choice(GUILDS) or "No Guild", bot_gold=bot["gold"],
        player_race=player["race"], player_gender=player["gender"], player_role=player["role"],
        player_faction=player["faction"], player_gold=player["gold"], distance=rng.randint(2, 25),
        bot_area=zone, bot_zone=zone, bot_map=ZONES[zone][3], gear_context=gear_ctx,
    )
    return {"messages": [{"role": "user", "content": prompt}, {"role": "assistant", "content": reply}]}


def gen_random_chatter_example(rng):
    pkey = rng.choice(list(P))
    ptext, style = P[pkey]
    bot = make_actor(rng)
    zone = pick_zone(rng, bot["level"], bot["f"])
    env_tmpl, env_replies = rng.choice(ENV_SITUATIONS)
    dun = [d for lo, hi, d in DUNGEONS_BY_LEVEL if lo <= bot["level"] + 3 and hi >= bot["level"] - 3]
    subs = {"creature": rng.choice(CREATURES), "quest_hub": rng.choice(QUEST_HUBS),
            "vendor": rng.choice(VENDORS), "dungeon": rng.choice(dun) if dun else "Deadmines"}
    env = env_tmpl.format(**subs)
    reply = apply_style(rng.choice(env_replies).format(**subs), style, rng)

    prompt = RANDOM_TEMPLATE.format(
        bot_name=bot["name"], bot_level=bot["level"], bot_class=bot["class"], bot_race=bot["race"],
        bot_gender=bot["gender"], bot_role=bot["role"], bot_faction=bot["faction"],
        bot_area=zone, bot_zone=zone, bot_map=ZONES[zone][3],
        bot_personality_name=pkey, bot_personality=ptext, environment=env,
    )
    return {"messages": [{"role": "user", "content": prompt}, {"role": "assistant", "content": reply}]}



# ---------------------------------------------------------------------------
# Guild-name generation examples ({guild_name} intent) - user turn mirrors
# OllamaChat_RenameGuildInVoice in mod-ollama-chat_guildnames.cpp exactly.
# ---------------------------------------------------------------------------

GUILDNAME_TEMPLATE = ("You're a Wrath-era WoW player. Name: {bot_name}, a level {bot_level} {bot_class}. "
                      "MAKE SURE YOU RESPOND USING YOUR PERSONALITY, WHICH IS: {pkey}: {ptext}. "
                      "You just founded a {archetype} guild. Invent its name: a memorable 2008-era WoW guild name, "
                      "2 to 4 words, plain text, no quotes, nothing else.")

GUILD_NAME_BANKS = {
    "HARDCORE_RAIDLEAD": ["Server First Or Wipe", "Mandatory Attendance", "Consumables Ready", "No Casuals Allowed"],
    "THEORYCRAFTER": ["Optimal Rotation", "Stat Weights United", "Simulated Victory", "The Spreadsheet"],
    "MIN_MAXER": ["Strictly Optimal", "Zero Waste Runs", "Best In Slot"],
    "LORE_NERD": ["Keepers of Lore", "The Sundering Scholars", "Archive of Azeroth"],
    "ELITE_ARENA_PVPER": ["Gladiator Or Bust", "Twenty Two Hundred", "Rating Farmers", "Trinket Bait"],
    "PVP_TRASHTALKER": ["Free Honor Kills", "Talk Is Cheap", "Graveyard Campers"],
    "DUELIST": ["First Blood Elite", "Outside Orgrimmar", "The Duelists Code"],
    "RAGER": ["Blind Fury", "Keyboard Smashers", "Anger Issues"],
    "WOW_MOM": ["The Cozy Hearth", "Family Dinner Table", "Warm Meals Guild", "Hugs And Heals"],
    "MENTOR": ["The Patient Blade", "Guiding Light", "Lessons Learned"],
    "GUILD_RECRUITER": ["Always Recruiting", "Join Us Today", "Open Invitations"],
    "CHILL_DAD": ["Weekend Warriors", "Dad Reflexes", "After The Kids Sleep"],
    "JOLLY_BEER_LOVER": ["Brews And Battles", "The Thirsty Kodo", "One More Round"],
    "HEROIC_LEADER": ["Banner Of Dawn", "Stand Together", "The Rallying Cry"],
}
GUILD_NAME_GENERIC = ["Azeroth Wanderers", "The Barrens Crew", "Midnight Raiders", "Crossroads Company"]
GUILD_ARCHETYPES = {"raid": ["HARDCORE_RAIDLEAD", "THEORYCRAFTER", "MIN_MAXER", "LORE_NERD"],
                    "pvp": ["ELITE_ARENA_PVPER", "PVP_TRASHTALKER", "DUELIST", "RAGER"],
                    "casual": ["WOW_MOM", "MENTOR", "GUILD_RECRUITER", "CHILL_DAD", "JOLLY_BEER_LOVER", "HEROIC_LEADER"]}

def gen_guild_name_example(rng):
    archetype = rng.choice(list(GUILD_ARCHETYPES))
    pkey = rng.choice(GUILD_ARCHETYPES[archetype])
    ptext = P.get(pkey, ("You lead a guild.", set()))[0]
    bot = make_actor(rng)
    prompt = GUILDNAME_TEMPLATE.format(bot_name=bot["name"], bot_level=max(bot["level"], 10),
                                       bot_class=bot["class"], pkey=pkey, ptext=ptext, archetype=archetype)
    name = rng.choice(GUILD_NAME_BANKS.get(pkey, GUILD_NAME_GENERIC))
    return {"messages": [{"role": "user", "content": prompt}, {"role": "assistant", "content": name}]}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default="dataset")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    seen, rows = set(), []
    while len(rows) < args.n:
        r = rng.random()
        ex = gen_guild_name_example(rng) if r < 0.03 else (gen_random_chatter_example(rng) if r < 0.28 else gen_chat_example(rng))
        key = (ex["messages"][0]["content"][:120], ex["messages"][1]["content"])
        if key in seen:
            continue
        seen.add(key)
        rows.append(ex)

    rng.shuffle(rows)
    n_eval = max(50, args.n // 50)
    with open(out / "train.jsonl", "w") as f:
        for r in rows[n_eval:]:
            f.write(json.dumps(r) + "\n")
    with open(out / "eval.jsonl", "w") as f:
        for r in rows[:n_eval]:
            f.write(json.dumps(r) + "\n")
    print(f"Wrote {len(rows) - n_eval} train / {n_eval} eval examples to {out}/")
    print("Sample reply:", rows[0]["messages"][1]["content"])


if __name__ == "__main__":
    main()
