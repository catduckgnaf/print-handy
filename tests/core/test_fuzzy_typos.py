"""Fuzzy phases 2-3: real misspellings resolve, real words stay as typed.

The phase-2 pairs are the curated set mined from the operator's own chat
history (aggregate misspelling -> correction counts only). The phase-3
groups add published common misspellings, keyboard-realistic variants
generated over the vocabulary's own target words, and one more mined
pair. Each pair must resolve through ``fuzzy.canonical`` and score as a
match; each real English word near the vocabulary must stay exactly as
typed.
"""
import pytest

from print_handy.bot import runtime
from print_handy.core import fuzzy
from print_handy.core.fuzzy_vocabulary import REAL_WORDS, TYPO_FORMS

CURATED_PAIRS = {
    "seperate": "separate", "separat": "separate", "maintance": "maintenance",
    "tempature": "temperature", "filement": "filament", "flament": "filament",
    "filame": "filament", "shrnik": "shrink", "supprots": "supports",
    "supporte": "support", "envirement": "environment", "restar": "restart",
    "stopp": "stop", "resum": "resume", "scal": "scale", "nozzl": "nozzle",
    "priht": "print", "printe": "print", "cance": "cancel",
}
VARIANTS = {"levelling": "leveling", "canceled": "cancel"}
CARD_TYPOS = {
    "nozzel": "nozzle", "filiment": "filament", "filamant": "filament",
    "calibarte": "calibrate", "callibrate": "calibrate", "purg": "purge",
    "extuder": "extruder", "retracion": "retraction", "brimm": "brim",
    "suport": "support", "spegetti": "spaghetti", "enviroment": "environment",
    "propbably": "probably", "alot": "a lot", "incase": "in case", "dept": "debt",
}
DO_NOT_MAP = ["stale", "later", "head", "point", "step", "shrine", "restriction",
              "warning", "pure", "spook", "micro", "calibre", "fitment", "stats",
              "states", "parse", "nest", "upstate", "fir"]
# Second mining pass over the operator's chat history (same aggregate-only
# rule: pairs, never message text). "iddint" maps to the apostrophe-less
# form the matcher canonicalizes to, and "seel" is kept deliberately: it is
# an archaic falconry word, not a living one, and the mined context is
# unambiguously "see". Real words found misspelled-looking in the same pass
# (nest, upstate, fir) were rejected, not mapped — see DO_NOT_MAP's rule.
OPERATOR_MINED_2 = {
    "ahow": "how", "catagories": "categories", "comouse": "compose",
    "conficts": "conflicts", "ddi": "did", "dobe": "done",
    "eleego": "elegoo", "iddint": "didnt", "incorperate": "incorporate",
    "intergration": "integration", "jewelery": "jewelry",
    "langauge": "language", "listerners": "listeners", "ocming": "coming",
    "recongized": "recognized", "rgar": "that", "sanatize": "sanitize",
    "seel": "see", "seperately": "separately", "spegattis": "spaghettis",
    "suppert": "support",
    "suprise": "surprise", "syntaxt": "syntax", "tempalates": "templates",
    "throughly": "thoroughly", "wel": "well", "whgatever": "whatever",
}
# Third mining pass: the operator typed both of these in one question on
# 2026-11-10 asking whether they were covered — they were not (0 hits in
# the repo, neither key in any table). Same aggregate-only rule: pairs,
# never message text. "mispellings" is the misspelling of "misspellings".
OPERATOR_MINED_3 = {
    "epsecially": "especially", "mispellings": "misspellings",
}
# Double-letter round (catduck, 2026-11-10, on PR #1191): more pairs
# whose correct spelling carries a doubled ss, rr or ll — the pattern
# behind both round-3 words ("especially", "misspellings"). Each typo
# drops one of the doubled letters, doubles the wrong neighbor, or
# triples the letter. Same curation rules as the rounds above: every
# key verified absent from TYPO_FORMS, TOKEN_FORMS and REAL_WORDS, not
# a real English word per pyspellchecker, and its word a real word
# carrying the double. Six candidates were rejected by that screen:
# "alow" and "instal" (real words), "eror" and "isue" (already in
# TOKEN_FORMS), "yelow" (already in the table), and "skil" -> "skill"
# (the lookup carry re-routes the word "skill" itself to "still", so
# the pair cannot be carried correctly — verified with no table entry
# present; that routing quirk is outside this round's scope).
DOUBLE_LETTER_TYPOS = {
    "acces": "access", "acess": "access", "acros": "across",
    "actualy": "actually", "addres": "address", "alowed": "allowed",
    "anualy": "annually", "arange": "arrange", "arive": "arrive",
    "arrrow": "arrow", "asembly": "assembly", "asess": "assess",
    "asist": "assist", "assesment": "assessment", "baloon": "balloon",
    "barier": "barrier", "borow": "borrow", "chalenge": "challenge",
    "clasroom": "classroom", "colect": "collect", "colege": "college",
    "controled": "controlled", "corect": "correct", "corected": "corrected",
    "corespond": "correspond", "coridor": "corridor", "curent": "current",
    "curently": "currently", "discusion": "discussion", "dolar": "dollar",
    "erors": "errors", "esential": "essential", "excelent": "excellent",
    "expres": "express", "folow": "follow", "folowing": "following",
    "fullly": "fully", "fuly": "fully", "galery": "gallery",
    "glas": "glass", "helo": "hello", "instaled": "installed",
    "inteligence": "intelligence", "leson": "lesson", "mesage": "message",
    "messsage": "message", "miror": "mirror", "mision": "mission",
    "overal": "overall", "paralell": "parallel", "pasword": "password",
    "permision": "permission", "posible": "possible", "presure": "pressure",
    "proces": "process", "profesional": "professional", "progres": "progress",
    "puley": "pulley", "referal": "referral", "sesion": "session",
    "smal": "small", "sory": "sorry",
    "speling": "spelling", "spelings": "spellings", "stil": "still",
    "succes": "success", "sucess": "success", "suround": "surround",
    "terible": "terrible", "teritory": "territory", "tisue": "tissue",
    "totaly": "totally", "usualy": "usually", "valey": "valley",
    "waranty": "warranty", "woried": "worried",
}
# Generic high-frequency non-word misspellings: domain typos the bounded
# edit-distance pass misses (filamet, nozel) plus classic English keyboard
# slips (teh, recieve, definately). Suggested in a branch commit whose file
# writes landed as placeholders; re-added here properly, pair by pair.
# Seven of these (infil, infiil, nozel, retracton, stringin, temperture,
# temprature) also exist in TOKEN_FORMS — the same deliberate duplication
# as cance/extuder/nozzl — because ``typo_fold`` consumers never see
# TOKEN_FORMS, so the TYPO entry is what folds them there.
GENERIC_TYPOS = {
    "adn": "and", "becasue": "because", "becuase": "because",
    "definately": "definitely", "definatly": "definitely",
    "extrudor": "extruder", "filamet": "filament", "filamnt": "filament",
    "infiil": "infill", "infil": "infill", "klipperr": "klipper",
    "listners": "listeners", "nozel": "nozzle", "occured": "occurred",
    "recieve": "receive", "reccomend": "recommend", "recomend": "recommend",
    "retraciton": "retraction", "retracton": "retraction",
    "seperatly": "separately", "stringin": "stringing", "teh": "the",
    "temperture": "temperature", "temprature": "temperature",
    "untill": "until", "warpping": "warping", "wich": "which",
}
# Phase 3, mined round 2: the operator asked for this one by name (the
# singular companion to the phase-2 spegattis -> spaghettis) and typed it
# again in the post-snapshot messages the second mining pass covered.
PHASE3_MINED = {
    "spegatti": "spaghetti",
}

# Phase 3: published common English misspellings (the classic
# high-frequency lists), each still held to the non-word rule - entries
# that turned out to be real words (calender, loosing) were dropped.
PHASE3_COMMON = {
    "accomodate": "accommodate", "accomodated": "accommodated", "accomodation": "accommodation",
    "acheive": "achieve", "acheived": "achieved", "adress": "address",
    "adressed": "addressed", "apparantly": "apparently", "arguement": "argument",
    "assasination": "assassination", "basicly": "basically", "beginer": "beginner",
    "beleive": "believe", "beleived": "believed", "carachter": "character",
    "cemetary": "cemetery", "changable": "changeable", "cheif": "chief",
    "collegue": "colleague", "commited": "committed", "completly": "completely",
    "concious": "conscious", "definate": "definite", "desireable": "desirable",
    "dilemna": "dilemma", "dissapear": "disappear", "dissapeared": "disappeared",
    "dissapoint": "disappoint", "dissapointed": "disappointed", "embarass": "embarrass",
    "embarassed": "embarrassed", "enviromental": "environmental", "equiptment": "equipment",
    "existance": "existence", "experiance": "experience", "experianced": "experienced",
    "familar": "familiar", "finaly": "finally", "fourty": "forty",
    "freind": "friend", "freinds": "friends", "gaurd": "guard",
    "goverment": "government", "grammer": "grammar", "greatful": "grateful",
    "happend": "happened", "harrass": "harass", "hierachy": "hierarchy",
    "humerous": "humorous", "ignor": "ignore", "immediatly": "immediately",
    "immitate": "imitate", "independant": "independent", "jelous": "jealous",
    "knowlege": "knowledge", "knowlegeable": "knowledgeable", "liason": "liaison",
    "libary": "library", "lonley": "lonely", "managable": "manageable",
    "millenium": "millennium", "mispell": "misspell", "morgage": "mortgage",
    "neccessary": "necessary", "neigbour": "neighbor", "nothern": "northern",
    "noticable": "noticeable", "occassion": "occasion", "occurence": "occurrence",
    "occurences": "occurrences", "ommited": "omitted", "opose": "oppose",
    "paralel": "parallel", "pasttime": "pastime", "pavillion": "pavilion",
    "peice": "piece", "persevear": "persevere", "posession": "possession",
    "posessive": "possessive", "prefered": "preferred", "pronounciation": "pronunciation",
    "publically": "publicly", "quantaty": "quantity", "questionaire": "questionnaire",
    "readible": "readable", "realy": "really", "recieveing": "receiving",
    "refered": "referred", "refering": "referring", "rember": "remember",
    "remberance": "remembrance", "requirment": "requirement", "rythm": "rhythm",
    "saftey": "safety", "secretery": "secretary", "sieze": "seize",
    "siezed": "seized", "similiar": "similar", "sincerly": "sincerely",
    "speach": "speech", "speachless": "speechless", "strenght": "strength",
    "succesful": "successful", "successfuly": "successfully", "supercede": "supersede",
    "tatoo": "tattoo", "tecnical": "technical", "tendancy": "tendency",
    "thier": "their", "threshhold": "threshold", "tommorow": "tomorrow",
    "tounge": "tongue", "transfered": "transferred", "truely": "truly",
    "underate": "underrate", "unforseeable": "unforeseeable", "unfortunatly": "unfortunately",
    "univesity": "university", "useage": "usage", "vaccum": "vacuum",
    "vaccuum": "vacuum", "vegeterian": "vegetarian", "vigilence": "vigilance",
    "wierd": "weird", "writen": "written", "writting": "writing",
    "yeild": "yield",
}

# Phase 3: keyboard-realistic variants generated over the vocabulary's
# own domain target words, bot command words and core printing terms
# (adjacent-key substitution, letter drop, doubling, transposition,
# vowel swap; at most four variants per target word). Ambiguous forms -
# plausibly a typo of two different words - were dropped, not guessed.
PHASE3_DOMAIN = {
    "aclibrate": "calibrate", "adheison": "adhesion", "adhesino": "adhesion",
    "adhesoin": "adhesion", "adhseion": "adhesion", "alyer": "layer",
    "apaghetti": "spaghetti", "apaghettis": "spaghettis", "bridgign": "bridging",
    "bridgnig": "bridging", "bridigng": "bridging", "brigding": "bridging",
    "caemra": "camera", "cailbrate": "calibrate", "calbirate": "calibrate",
    "calibraiton": "calibration", "calibratino": "calibration", "calibratoin": "calibration",
    "calibrtaion": "calibration", "camear": "camera", "camrea": "camera",
    "claibrate": "calibrate", "clgoging": "clogging", "cloggign": "clogging",
    "cloggnig": "clogging", "clogigng": "clogging", "cmaera": "camera",
    "confgiure": "configure", "configrue": "configure", "configuer": "configure",
    "configuraiton": "configuration", "configuratino": "configuration", "configuratoin": "configuration",
    "configurtaion": "configuration", "confiugre": "configure", "cooilng": "cooling",
    "coolign": "cooling", "coolnig": "cooling", "detacehd": "detached",
    "detachde": "detached", "detahced": "detached", "detcahed": "detached",
    "detcetor": "detector", "deteciton": "detection", "detecotr": "detector",
    "detectino": "detection", "detectoin": "detection", "detectro": "detector",
    "detetcion": "detection", "detetcor": "detector", "dpaghetti": "spaghetti",
    "dpaghettis": "spaghettis", "eetraction": "retraction", "ehight": "height",
    "ertraction": "retraction", "etraction": "retraction", "exrtuder": "extruder",
    "extrduer": "extruder", "extrduing": "extruding", "extrsuion": "extrusion",
    "extrudnig": "extruding", "extrudre": "extruder", "extruedr": "extruder",
    "extruidng": "extruding", "extruison": "extrusion", "extrusino": "extrusion",
    "extrusoin": "extrusion", "exturding": "extruding", "failrue": "failure",
    "failrues": "failures", "failuers": "failures", "failurse": "failures",
    "faiulre": "failure", "faiulres": "failures", "faliure": "failure",
    "fetraction": "retraction", "fialure": "failure", "filaemnt": "filament",
    "filametn": "filament", "filamont": "filament", "gcdoe": "gcode",
    "gcoda": "gcode", "gcoed": "gcode", "gocde": "gcode",
    "heaitng": "heating", "heatign": "heating", "heatnig": "heating",
    "hegiht": "height", "heigth": "height", "heihgt": "height",
    "iflament": "filament", "iwdth": "width", "laeyr": "layer",
    "laeyrs": "layers", "layesr": "layers", "layre": "layer",
    "layres": "layers", "leveilng": "leveling", "levelign": "leveling",
    "levelnig": "leveling", "levleing": "leveling", "lsiced": "sliced",
    "lsicer": "slicer", "lyaer": "layer", "lyaers": "layers",
    "mainteannce": "maintenance", "maintenacne": "maintenance", "maintenanec": "maintenance",
    "maintennace": "maintenance", "nozlze": "nozzle", "nozzla": "nozzle",
    "nzozle": "nozzle", "ocoling": "cooling", "onzzle": "nozzle",
    "ooizng": "oozing", "oozign": "oozing", "ooznig": "oozing",
    "ovehrang": "overhang", "overahng": "overhang", "overhagn": "overhang",
    "ovrehang": "overhang", "ozoing": "oozing", "paghetti": "spaghetti",
    "paghettis": "spaghettis", "porfile": "profile", "prfoile": "profile",
    "priimng": "priming", "primign": "priming", "primnig": "priming",
    "prmiing": "priming", "profiel": "profile", "profiels": "profiles",
    "profilse": "profiles", "proflies": "profiles", "proifle": "profile",
    "proifles": "profiles", "psaghetti": "spaghetti", "psaghettis": "spaghettis",
    "pseed": "speed", "pseeds": "speeds", "purgi": "purge",
    "purgo": "purge", "purgu": "purge", "rertact": "retract",
    "retarct": "retract", "retratc": "retract", "retrcat": "retract",
    "satlled": "stalled", "seped": "speed", "sepeds": "speeds",
    "setitngs": "settings", "settigns": "settings", "settinsg": "settings",
    "settnigs": "settings", "silced": "sliced", "silcer": "slicer",
    "slciing": "slicing", "slicar": "slicer", "slicde": "sliced",
    "slicign": "slicing", "slicnig": "slicing", "sliecd": "sliced",
    "sliecr": "slicer", "sliicng": "slicing", "snaphsot": "snapshot",
    "snapshto": "snapshot", "snapsoht": "snapshot", "snasphot": "snapshot",
    "spede": "speed", "spedes": "speeds", "speesd": "speeds",
    "speid": "speed", "staleld": "stalled", "stallde": "stalled",
    "stlaled": "stalled", "strigning": "stringing", "stringign": "stringing",
    "stringnig": "stringing", "strinigng": "stringing", "tarvel": "travel",
    "temperatrue": "temperature", "temperatuer": "temperature", "temperautre": "temperature",
    "tempertaure": "temperature", "timealpse": "timelapse", "timelapes": "timelapse",
    "timelaspe": "timelapse", "timelpase": "timelapse", "traevl": "travel",
    "travle": "travel", "troublehsoot": "troubleshoot", "troubleshooitng": "troubleshooting",
    "troubleshootign": "troubleshooting", "troubleshootnig": "troubleshooting", "troubleshoto": "troubleshoot",
    "troubleshotoing": "troubleshooting", "troublesohot": "troubleshoot", "troublsehoot": "troubleshoot",
    "trvael": "travel", "uprge": "purge", "wadth": "width",
    "wapring": "warping", "waripng": "warping", "warpign": "warping",
    "watcdhog": "watchdog", "watchdgo": "watchdog", "watchodg": "watchdog",
    "wathcdog": "watchdog", "wedth": "width", "wiipng": "wiping",
    "wipign": "wiping", "wipnig": "wiping", "witdh": "width",
    "wpiing": "wiping", "wraping": "warping",
}

# Phase 3: the same generation over the remaining dictionary vocabulary
# words from TOKEN_FORMS values and PHRASE_FORMS words.
PHASE3_GENERATED = {
    "abmboo": "bamboo", "acble": "cable", "acceleraiton": "acceleration",
    "acceleratino": "acceleration", "acceleratoin": "acceleration", "accelermoeter": "accelerometer",
    "acceleroemter": "accelerometer", "acceleromeetr": "accelerometer", "acceleromteer": "accelerometer",
    "accelertaion": "acceleration", "adavnce": "advance", "advacne": "advance",
    "advanec": "advance", "adventruer": "adventurer", "adventuerr": "adventurer",
    "adventurre": "adventurer", "advenutrer": "adventurer", "advnace": "advance",
    "aluimnum": "aluminum", "aluminmu": "aluminum", "alumiunm": "aluminum",
    "alumnium": "aluminum", "analyez": "analyze", "analzye": "analyze",
    "anaylze": "analyze", "anlayze": "analyze", "aotmic": "atomic",
    "aqiula": "aquila", "aquial": "aquila", "aqulia": "aquila",
    "artilelry": "artillery", "artilleyr": "artillery", "artillrey": "artillery",
    "artlilery": "artillery", "assisatnt": "assistant", "assistatn": "assistant",
    "assistnat": "assistant", "assitsant": "assistant", "atmoic": "atomic",
    "atoimc": "atomic", "atomci": "atomic", "auqila": "aquila",
    "auxiilary": "auxiliary", "auxilairy": "auxiliary", "auxiliayr": "auxiliary",
    "auxiliray": "auxiliary", "babmoo": "bamboo", "backalsh": "backlash",
    "backlahs": "backlash", "backlsah": "backlash", "baclkash": "backlash",
    "baecon": "beacon", "beacno": "beacon", "beaocn": "beacon",
    "becaon": "beacon", "behaivor": "behavior", "behaviro": "behavior",
    "behavoir": "behavior", "behvaior": "behavior", "bemboo": "bamboo",
    "berak": "break", "biemtal": "bimetal", "bimeatl": "bimetal",
    "bimetla": "bimetal", "bimteal": "bimetal", "birdge": "bridge",
    "biuld": "build", "blcok": "block", "blokc": "block",
    "bmaboo": "bamboo", "boadr": "board", "boerd": "board",
    "bolck": "block", "borad": "board", "braek": "break",
    "brdige": "bridge", "breakotu": "breakout", "breakuot": "breakout",
    "breaokut": "breakout", "breka": "break", "brekaout": "breakout",
    "brideg": "bridge", "brigde": "bridge", "buidl": "build",
    "bulid": "build", "cabla": "cable", "cailber": "caliber",
    "calbe": "cable", "calbier": "caliber", "caliebr": "caliber",
    "capacitiev": "capacitive", "capacitvie": "capacitive", "cbale": "cable",
    "claiber": "caliber", "lbock": "block", "obard": "board",
    "rbeak": "break", "ubild": "build",
}
# Batch 2 (#1017): curated two-edit misspellings of print-title words.
# The bounded pass reaches one edit for these token lengths, so each
# pair here is exactly two Damerau-Levenshtein edits from its word,
# validated unambiguous against the whole vocabulary (see
# fuzzy_vocabulary's TYPO_FORMS note).
BATCH2_TITLE_TYPOS = {
    "aadpetr": "adapter", "aadptre": "adapter", "barcekt": "bracket",
    "barckte": "bracket", "bbax": "box", "bbxo": "box",
    "becnyh": "benchy", "benchie": "benchy", "calba": "cable",
    "cbael": "cable", "ccilp": "clip", "cclpi": "clip",
    "darewr": "drawer", "dargno": "dragon", "darogn": "dragon",
    "darwre": "drawer", "ecnlosrue": "enclosure", "ecnlosuer": "enclosure",
    "fdiegt": "fidget", "fdigte": "fidget", "gaa": "gear",
    "gaerr": "gear", "ghso": "ghost", "gohts": "ghost",
    "haneg": "hinge", "haokk": "hook", "hemlte": "helmet",
    "hhoko": "hook", "hleemt": "helmet", "hlodre": "holder",
    "hloedr": "holder", "hnieg": "hinge", "kecyahin": "keychain",
    "kecyhani": "keychain", "maaks": "mask", "mautn": "mount",
    "msa": "mask", "muotn": "mount", "ograinzer": "organizer",
    "ograniezr": "organizer", "palnetr": "planter", "palntre": "planter",
    "pmukpin": "pumpkin", "pmupikn": "pumpkin", "raakc": "rack",
    "rca": "rack", "rcoekt": "rocket", "rcokte": "rocket",
    "satdn": "stand", "satndd": "stand", "satrr": "star",
    "sklu": "skull", "sowrdd": "sword", "ssatr": "star",
    "sukl": "skull", "swro": "sword", "tawre": "tower",
    "tewre": "tower", "vsaa": "vase", "vsai": "vase",
}
ALL_PAIRS = {**CURATED_PAIRS, **VARIANTS, **CARD_TYPOS, **OPERATOR_MINED_2,
             **OPERATOR_MINED_3, **DOUBLE_LETTER_TYPOS,
             **GENERIC_TYPOS, **PHASE3_MINED, **PHASE3_COMMON,
             **PHASE3_DOMAIN, **PHASE3_GENERATED, **BATCH2_TITLE_TYPOS}


@pytest.mark.parametrize("typo,word", sorted(ALL_PAIRS.items()))
def test_every_pair_is_in_the_table(typo, word):
    assert TYPO_FORMS[typo] == word


@pytest.mark.parametrize("typo,word", sorted(ALL_PAIRS.items()))
def test_every_pair_folds_to_its_word(typo, word):
    assert fuzzy.typo_fold(typo) == word
    assert fuzzy.canonical(typo) == fuzzy.canonical(word)


@pytest.mark.parametrize("typo,word", sorted(ALL_PAIRS.items()))
def test_every_pair_scores_as_a_match(typo, word):
    assert fuzzy.fuzzy_score(typo, word) == 99


@pytest.mark.parametrize("word", DO_NOT_MAP)
def test_real_words_are_never_rewritten(word):
    assert word not in TYPO_FORMS
    assert word in REAL_WORDS
    assert fuzzy.typo_fold(word) == word
    vocabulary = set(TYPO_FORMS.values()) | {"layer", "scale", "heat", "print", "stop",
                                             "states", "status", "shrink", "filament"}
    assert fuzzy.correct_token(word, vocabulary) == word
    assert fuzzy.correct_typos(word, vocabulary) == word


@pytest.mark.parametrize("real,near", [("stale", "scale"), ("later", "layer"),
                                       ("point", "print"), ("stats", "status"),
                                       ("fitment", "filament")])
def test_real_words_do_not_score_as_their_neighbour(real, near):
    assert fuzzy.fuzzy_score(real, near) < 99


@pytest.mark.parametrize("text,word", [
    ("shrnik it by 20%", "shrink"), ("can you add supprots", "supports"),
    ("seperate the bed and nozzle", "separate"), ("do some maintance", "maintenance"),
])
def test_phrases_fold_in_context(text, word):
    assert word in fuzzy.typo_fold(text).split()


def test_two_word_split_is_repaired():
    assert fuzzy.typo_fold("youa bout") == "you about"


# ------------------------------------------------- bounded edit distance

@pytest.mark.parametrize("a,b,distance", [
    ("shrnik", "shrink", 1), ("nozzel", "nozzle", 1), ("kitten", "sitting", 3),
    ("maintenence", "maintenance", 1), ("calibrtion", "calibration", 1),
])
def test_edit_distance_counts_transpositions_as_one(a, b, distance):
    assert fuzzy.edit_distance(a, b, 3) == distance


def test_edit_distance_stops_past_the_limit():
    assert fuzzy.edit_distance("abcdefgh", "zzzzzzzz", 2) == 3


def test_edit_distance_long_tokens_bounded():
    assert fuzzy.edit_distance("a" * 2000, "a" * 1999 + "b", 2) == 1
    assert fuzzy.windowed_fuzzy_score("a" * 2000, "a" * 1999 + "b") == 99


@pytest.mark.parametrize("token,limit", [("bed", 0), ("stop", 0), ("purge", 1),
                                         ("nozzle", 1), ("extrude", 1),
                                         ("filament", 2), ("calibration", 2),
                                         ("a" * 50, 0)])
def test_typo_limit_by_length(token, limit):
    assert fuzzy.typo_limit(token) == limit


@pytest.mark.parametrize("token,expected", [
    ("tempreture", "temperature"),   # 10 letters, 2 edits
    ("maintenence", "maintenance"),  # 11 letters, 1 edit
    ("nozlze", "nozzle"),            # 6 letters, 1 transposition
    ("calibrtion", "calibration"),
])
def test_correct_token_within_bounds(token, expected):
    vocabulary = {"temperature", "maintenance", "nozzle", "calibration", "filament"}
    assert fuzzy.correct_token(token, vocabulary) == expected


@pytest.mark.parametrize("token", [
    "nozz",      # 4 letters: no tolerance
    "mozzle",    # first letter differs
    "nzzole",    # first letter matches but 2 edits at 6 letters
    "tmprtr",    # too far
    "ab12cd",    # not a plain word
])
def test_correct_token_outside_bounds_is_untouched(token):
    assert fuzzy.correct_token(token, {"nozzle", "temperature"}) == token


def test_correct_token_ties_are_left_alone():
    # "spoal" is one edit from both "spool" and "spoil": no guess.
    assert fuzzy.correct_token("spoal", {"spool", "spoil"}) == "spoal"


def test_correct_typos_keeps_ids_urls_and_numbers():
    text = "check the nozzel @<123> https://x.example/a.3mf 0.4mm"
    assert fuzzy.correct_typos(text, {"nozzle"}) == "check the nozzle @<123> https://x.example/a.3mf 0.4mm"


def test_fuzzy_score_tolerates_typos_against_the_target():
    assert fuzzy.fuzzy_score("tempreture tower", "temperature tower") == 99
    assert fuzzy.fuzzy_score("calibrtion cube", "Calibration Cube") == 99


# --------------------------------------------------------- bot routing

@pytest.mark.parametrize("text,command", [
    ("seperate the bed and nozzle", "/print-bed-down"),
    ("do some maintance", "/print-maintenance"),
    ("do the maintance", "/print-maintenance"),
    ("change filiment", "/spool"),
    ("restar the printer", "/print-reboot"),
    ("resum the print", "/print-resume"),
    ("stopp the print", "/print-stop"),
    ("the nozzel is clogged", "/print-troubleshoot clog"),
    ("change the nozzel", "/nozzle change"),
    ("callibrate the bed", "/print-calibrate"),
])
def test_misspelled_tasks_get_the_right_suggestion(text, command):
    assert runtime.command_suggestion(text) == command


@pytest.mark.parametrize("text", ["later", "stale", "point", "step", "the head", "warning"])
def test_real_words_get_no_task_suggestion(text):
    assert runtime.command_suggestion(text) is None


def test_typo_routing_is_only_a_suggestion():
    # The suggestion is a string the user confirms; nothing executes here.
    assert isinstance(runtime.command_suggestion("stopp the print"), str)



# ------------------------------ Phase 3 collision shields and mined typos (#1002)

PHASE3_COLLISION_SHIELDS = [
    ("statue", "status"),
    ("spring", "string"),
    ("slide", "slice"),
    ("stock", "stuck"),
    ("combing", "coming"),
    ("cracking", "clicking"),
]


@pytest.mark.parametrize("real,near", PHASE3_COLLISION_SHIELDS)
def test_phase3_real_words_collision_shields(real, near):
    assert real in REAL_WORDS
    assert real not in TYPO_FORMS
    assert fuzzy.typo_fold(real) == real
    assert fuzzy.correct_token(real, {near}) == real
    assert fuzzy.correct_typos(real, {near}) == real
    assert fuzzy.fuzzy_score(real, near) < 99


PHASE3_MINED_TYPOS = {
    "becnhy": "benchy",
    "dragn": "dragon",
    "braket": "bracket",
    "spegatti": "spaghetti",
    "adheasion": "adhesion",
    "hotnd": "hotend",
    "extrudr": "extruder",
    "cancle": "cancel",
    "pauze": "pause",
    "rost": "roast",
    "raost": "roast",
    "repirnt": "reprint",
}


@pytest.mark.parametrize("typo,word", sorted(PHASE3_MINED_TYPOS.items()))
def test_phase3_mined_typos_in_table_and_fold(typo, word):
    assert TYPO_FORMS[typo] == word
    assert fuzzy.typo_fold(typo) == word
    assert fuzzy.canonical(typo) == fuzzy.canonical(word)
    assert fuzzy.fuzzy_score(typo, word) == 99
