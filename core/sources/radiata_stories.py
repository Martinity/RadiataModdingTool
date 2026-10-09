'''
source definitions for Radiata Stories on the PS2
'''
from __future__ import annotations

import struct
from typing import Iterator, Any, TYPE_CHECKING
from functools import lru_cache
from struct import unpack_from
from enum import auto

from core.registry import Registry
from core.contracts import BaseSource
from core.iso_layout import IsoSourceBuilder
from core.handlers.kods_container import HEADER_ROLE, slot_header_role
from core.extension_overrides import lookup_extension
from core.node import VfsNode

if TYPE_CHECKING:
    from core.metadata_manager import StaticMetadataSource
    from core.native.block_device import BlockDevice

_GEOMETRY = BaseSource.Geometry(sector_size=0x800, iso_9660_pvd=16, pvd_byte_offset=0x9C)
_RUNTIME_REQUIRED_FILES = {'IOPRP300', 'SYSTEM'}
_RUNTIME_EXECUTABLE_CANDIDATES = {'SLUS_212', 'SLPM_658'}

###----------------------------------- TOC ---------------------------------------###

_SEPARATOR  = b'\x2D\x20\x20\x02'
_STATIC_LUI = b'\x02\x3C'
_STATIC_ORI = b'\x42\x34'

def _find_masked_immediate(data: bytes) -> int | None:
    ''''''
    pos = 0
    while pos < (len(data) - 8):
        pos = data.find(_SEPARATOR, pos)
        if pos == -1:
            return None
        if pos & 3:
            pos += 1
            continue
        if data[pos - 2:pos] == _STATIC_LUI and data[pos + 6:pos + 8] == _STATIC_ORI:
            return pos
        pos += 4
    return None

def _read_masked_immediate_value(data: bytes, pos: int) -> int:
    hi_val = int.from_bytes(data[pos - 4:pos - 2], 'little')
    lo_val = int.from_bytes(data[pos + 4:pos + 6], 'little')
    return (hi_val << 16) | lo_val

def _write_masked_immediate_value(data: bytearray, pos: int, value: int) -> None:
    hi_val = (value >> 16) & 0xFFFF
    lo_val = value & 0xFFFF
    data[pos - 4:pos - 2] = struct.pack('<H', hi_val)
    data[pos + 4:pos + 6] = struct.pack('<H', lo_val)

###---------------------------------------- Metadata ---------------------------------------------###

# System and Streamed files (0–206)
SYSTEM_LABELS = {
    0: "ToC Index", 1: "Iop Modules", 2: "CRadiApp Overlay", 3: "Sfx", 4: "Ank Font",
    5: "Data Center Index", 6: "Mc Save Icon", 7: "Hdd Browser Meta",
    8: "Transpiritation", 9: "Earth Dragon", 10: "Silver Dragon Falls",
    11: "Cross Invades Earth Valley", 12: "Dolby Surround Pro Logic II",
    13: "Title", 14: "Opening", 15: "Ending Human", 16: "Ending Nonhuman",
    17: "Empty Slot", 18: "Character Roster", 19: "Monster Data",
    20: "Empty Placeholder", 21: "Item Tier Table", 22: "Empty Placeholder 2",
    23: "Item Property Table", 24: "Name Table", 25: "Unknown 0MPA",
    26: "Font Encoding", 27: "Step1 00 Save Load", 28: "Step1 01 Overworld",
    29: "Step1 02 Battle", 30: "Step2 00 Party", 31: "Step2 01 Victory",
    32: "Step2 99", 33: "Debug Route Editor", 34: "Debug Chara Checker",
    35: "Debug Rmf Checker", 36: "Debug Hoshi", 37: "Debug Kushi", 38: "Debug Nishi",
    39: "Debug Yoko", 40: "Debug Kame", 41: "T10000 Tools", 42: "Diffuse World",
    43: "Special Grace", 44: "Opinion Leaders Values", 45: "Colosseum",
    46: "Silent Way", 47: "Hopping Sun", 48: "Song Of Freedom Fighters",
    49: "Magical World", 50: "The White Town Of Deception", 51: "Outsiders",
    52: "Perpetual Unsteadiness", 53: "Feelings That Span The Distance",
    54: "Demise Of Paradise", 55: "Idling Idol", 56: "No Graffiti",
    57: "Artisan", 58: "Unknown", 59: "Instant Talk Back", 60: "Selfish Raider",
    61: "Atrophy And Inspiration", 62: "Underground Grinder",
    63: "Unknown And Unnamed Spot", 64: "A Human And An Inhuman",
    65: "An Approaching Menace", 66: "Struggle For Life", 67: "Fatal Damage",
    68: "Death Trap Refrain", 69: "Struggle 1", 70: "Struggle 2", 71: "Struggle 3",
    72: "Powerful Enemy", 73: "Only For You", 74: "Gloomy Dance",
    75: "Lost Lost And Lost", 76: "Honky Tonk Boy", 77: "Genuine Girl",
    78: "Bloom Of Anxiety", 79: "Go Straight With My Brave", 80: "Teach Me Please",
    81: "Teach Me Why", 82: "Labyrinth Of Fortune", 83: "Invitation",
    84: "Calm Melody", 85: "Plod Along Karaoke Ver", 86: "Devote For Nature",
    87: "Exotic Exhaust", 88: "Airy Feathers", 89: "Scarlet Wind", 90: "Mens Dirge",
    91: "Paya Paya", 92: "The Waterfalls And Whirlpools Of Conscienceness",
    93: "Take My Way", 94: "Maybe Thats True", 95: "Itinerant Party",
    96: "Gratifying Guest", 97: "Night Memories", 98: "The Boundary", 99: "To The Full",
    100: "War Intermezzo", 101: "Only Some People For Now", 102: "Legendary Sword",
    103: "Yearning For Brilliance A Flower Blooms", 104: "Opinion Leaders Values",
    105: "Colosseum", 106: "Colosseum Partial Ver", 107: "Hopping Sun Partial Ver",
    108: "Song Of Freedom Fighters Partial Ver", 109: "Magical World Partial Ver",
    110: "The White Town Of Deception Partial Ver", 111: "Outsiders Partial Ver",
    112: "Perpetual Unsteadiness Partial Ver", 113: "Demise Of Paradise High Tempo Ver",
    114: "Instant Talk Back High Tempo Ver", 115: "Labyrinth Of Fortune Partial Ver",
    116: "Plod Along Karaoke Ver", 117: "Paya Paya Karaoke Ver",
    118: "01", 119: "02 1", 120: "02 2", 121: "03", 122: "04", 123: "05", 124: "06",
    125: "07", 126: "08", 127: "09", 128: "10", 129: "11", 130: "12", 131: "13",
    132: "14", 133: "15", 134: "16", 135: "17", 136: "18", 137: "19", 138: "20",
    139: "21", 140: "22", 141: "23", 142: "24", 143: "25", 144: "26", 145: "27",
    146: "28", 147: "29", 148: "30", 149: "31", 150: "32", 151: "33", 152: "34",
    153: "35", 154: "36", 155: "37", 156: "38", 157: "39", 158: "40", 159: "41",
    160: "42", 161: "43", 162: "44", 163: "45", 164: "46",
    165: "Legendary Sword Loop Ver", 166: "48", 167: "49",
    168: "Mission To The Deep Space Radiata Ver", 169: "The Incarnation Of The Devil",
    170: "A Closed Door", 171: "Highbrow", 172: "Awesome But Invisible One",
    173: "Billboard Attack", 174: "Fortune", 175: "Plod Along Loop Ver",
    176: "Voice Bank Overworld", 177: "Voice Bank Battle Voices",
    178: "Voice Bank Battle Grunts", 179: "Voice Bank Volty", 180: "Voice Bank Story",
    181: "Vibration Data", 182: "Chara Put Group", 183: "Technique Data",
    184: "Schedule Script 1", 185: "Schedule Script 2", 186: "Event Script",
    187: "Event Script Secondary", 188: "Sound Bank 5", 189: "Texture Bank 00",
    190: "Texture Bank 01", 191: "Texture Bank 02", 192: "Texture Bank 03",
    193: "Texture Bank 04", 194: "Texture Bank 05", 195: "Texture Bank 06",
    196: "Texture Bank 07", 197: "Texture Bank 08", 198: "Texture Bank 09",
    199: "Texture Bank 10", 200: "Texture Bank 11", 201: "Texture Bank 12",
    202: "File 0202", 203: "Paf Data", 204: "Ctalk Messages",
    205: "Character Portraits", 206: "Script Modules",
}

# Area / map scripts (207–1205)
AREA_LABELS = {
    207: "Nuevo Village", 208: "Septem Region-Adien Region", 209: "Dova Region-Adien Region",
    210: "Dorse Region-Adien Region", 211: "Ocho Region-Adien Region", 212: "Adele's Residence",
    213: "Dorse Region-Adien Region", 214: "Adien Region", 215: "Area 01 Plains", 216: "Adien Region", 217: "Adien Region",
    218: "Dwarf Tunnel", 219: "Dova Region", 220: "Dova Region", 221: "Area 02 Tunnel Worm",
    222: "Area 02 Tunnel Resting Spot", 223: "Area 02 Singed Plains", 224: "Area 02 Tunnel",
    225: "Area 02 Dwarf Village Square", 226: "Earth Valley", 227: "Elder's Residence 1st Floor",
    228: "Elder's Residence 2nd Floor", 229: "Elder's Residence 3rd Floor",
    230: "Dwarfun General Store", 231: "Triston Armory", 232: "Boulder Frog Inn",
    233: "Boulder Frog Inn Hall", 234: "Boulder Frog Inn Room 101",
    235: "Boulder Frog Inn Room 102", 236: "Vashtel Liquor Store", 237: "Dawnbay Diner",
    238: "Treasury", 239: "Blacksmith Dyvad", 240: "Blacksmith Brockle",
    241: "Blacksmith Gehrmann", 242: "Sergei's Place", 243: "Dormitory Room 101",
    244: "Dormitory", 245: "Dormitory Room 102", 246: "Marke's Place", 247: "Map Name",
    248: "0248 (Dwarf Tunnel)", 249: "0249", 250: "0250", 251: "0251", 252: "Dova Region",
    253: "Dova Region (Singed plains)", 254: "Dova Region (Singed plains)", 255: "Dova Region", 256: "0256", 257: "0257",
    258: "0258", 259: "0259", 260: "Cuatour Region", 261: "Cuatour Region",
    262: "Area 02 Plains", 263: "Area 02 Cliff", 264: "Area 02 Mountain Path", 265: "Area 04 Village Square",
    266: "Cuatour Region", 267: "0267", 268: "Tria Village", 269: "Cuatour Region",
    270: "Tria Village", 271: "Map 065", 272: "Nowem Region-Cuatour Region",
    273: "Elf Region-Cuatour Region", 274: "Radiata Faucon Gate Entrance",
    275: "Area 04 Plains", 276: "Elder's Residence Entrance", 277: "Elder's Residence Kitchen",
    278: "Elder's Residence Livingroom", 279: "Elder's Residence Bedroom",
    280: "Tarkin's Residence Livingroom", 281: "Tarkin's Residence Bedroom",
    282: "Barn 1st Floor", 283: "Barn 2nd Floor", 284: "0284",
    285: "Dysett Region-Septem Region", 286: "Septem Region", 287: "Septem Cave",
    288: "Septem Cave", 289: "Septem Cave", 290: "Septem Cave", 291: "Septem Cave",
    292: "Septem Region", 293: "Septem Cave", 294: "Septem Cave", 295: "Septem Region",
    296: "Area 07 Cave (with water)", 297: "Area 07 Cave (no water)", 298: "Area 07 Cave Lake Bottom",
    299: "Area 07 Savanna", 300: "Area 07 Cliff", 301: "Map 095", 302: "Septem Region",
    303: "07 Castle Summoning Room", 304: "Algandars Castle", 305: "Algandars Castle",
    306: "Algandars Castle", 307: "Algandars Castle", 308: "Algandars Castle",
    309: "Algandars Castle", 310: "Algandars Castle", 311: "Algandars Castle",
    312: "Algandars Castle", 313: "Algandars Castle", 314: "Algandars Castle",
    315: "Algandars Castle", 316: "Algandars Castle", 317: "Algandars Castle",
    318: "Area 16 Cliff", 319: "Desneuf Region", 320: "Borgandiazo", 321: "Borgandiazo",
    322: "Borgandiazo", 323: "Borgandiazo", 324: "Borgandiazo", 325: "Borgandiazo",
    326: "Dorse Region", 327: "Dorse Region", 328: "Shangri La", 329: "Shangri La",
    330: "Shangri La", 331: "Shangri La", 332: "Algandars Castle", 333: "Shed",
    334: "Area 07 Castle Room", 335: "Area 12 Jungle", 336: "Dungeon Passage", 337: "Dungeon Passage",
    338: "Dungeon Stairs", 339: "Dungeon Passage", 340: "Radiata Castle Big Tower",
    341: "Coliseum", 342: "Radiata Castle Basement 1st Floor", 343: "Guards' Room",
    344: "Guards' Room", 345: "Guards' Room", 346: "Radiata Castle 1st Floor Hall",
    347: "Lounge", 348: "Radiata Castle 1st Floor Hall", 349: "Wind Power Elevator", 350: "Radiata Castle 1st Floor Hall",
    351: "Information", 352: "Men's Toilet", 353: "Mook",
    354: "Radiata Castle 1st Floor Hall", 355: "Radiata Castle North Gate",
    356: "Radiata Castle 2nd Floor Hall", 357: "Employee's Room", 358: "Employee's Room",
    359: "Kitchen", 360: "Banquet Hall", 361: "Library",
    362: "Radiata Castle 2nd Floor Hall", 363: "Radiata Castle 2nd Floor Hall",
    364: "Radiata Castle 3rd Floor Hall", 365: "Employee's Room", 366: "Radiata Castle Exterior",
    367: "Map 161", 368: "Radiata Castle 3rd Floor Hall",
    369: "Trainee's Room", 370: "Trainee's Room",
    371: "Radiata Castle Basement 1st Floor", 372: "Radiata Castle Small Tower",
    373: "Map 167", 374: "Radiata Castle 3rd Floor Hall",
    375: "Radiata Castle 4th Floor Hall", 376: "Radiata Castle Supply Store",
    377: "Ridley's Room", 378: "Radiata Castle 2nd Floor Hall",
    379: "Radiata Castle South Gate", 380: "Lark's Room", 381: "Lark's Bedroom",
    382: "Private House", 383: "Study", 384: "Cross's Room", 385: "Luciana's Room",
    386: "Radiata Castle 3rd Floor Hall", 387: "Ganz's Room",
    388: "Knight Meeting Room", 389: "Dynas's Room",
    390: "Radiata Castle 4th Floor Hall", 391: "Radiata Castle 4th Floor Hall",
    392: "Radiata Castle 4th Floor Hall", 393: "Jasne's Room",
    394: "Royal Family Exclusive Elevator", 395: "Royal Family Exclusive Elevator",
    396: "Radiata Castle 5th Floor Hall", 397: "Radiata Castle 5th Floor Hall",
    398: "Conference Hall", 399: "Jioruss Room", 400: "Jiorus's Bedroom",
    401: "Jiorus's Closet", 402: "Sarasenia's Room", 403: "Sarasenia's Bedroom",
    404: "Belflower's Room", 405: "Belflower's Bedroom",
    406: "Audience Chamber", 407: "Radiata Castle 5th Floor Hall",
    408: "Map 202", 409: "Radiata Castle Basement 1st Floor",
    410: "Radiata Castle Basement 1st Floor", 411: "Radiata Castle Basement 1st Floor",
    412: "Radiata Castle Basement 1st Floor", 413: "Radiata Castle Basement 1st Floor",
    414: "Training Facility", 415: "Storeroom",
    416: "Radiata Castle Basement 1st Floor", 417: "Waiting Room", 418: "Waiting Room",
    419: "Waiting Room", 420: "Waiting Room", 421: "Sakurazaki's Room",
    422: "Junzaburo's Room", 423: "Coliseum Passage",
    424: "Radiata Castle Basement 1st Floor", 425: "Storeroom",
    426: "Dichett Region", 427: "Fire Mountain", 428: "Parsec's Chamber",
    429: "Fire Mountain Crater", 430: "Vaultroom", 431: "Ballroom",
    432: "Lockup", 433: "Lockup", 434: "Lockup",
    435: "Area 34 Fire Mountain", 436: "Algandars Castle", 437: "Algandars Castle",
    438: "Algandars Castle", 439: "Algandars Castle", 440: "Summoning Room",
    441: "Area 03 Boulder Plains", 442: "Desneuf Region Ocho Region", 443: "Ocho Region",
    444: "Ocho Region", 445: "Ocho Region", 446: "City Of Flowers",
    447: "Area 11 Light Elf Village", 448: "Private House", 449: "Private House",
    450: "City Of Flowers Meeting Area", 451: "Private House",
    452: "Elder's Residence", 453: "Private House",
    454: "Elf Region", 455: "Elf Region", 456: "Elf Region",
    457: "Area 16 Orc Village Interior", 458: "Area 16 Orc Village Outer Wall",
    459: "Area 16 Orc Village Bottom Floor", 460: "Dichett Region-Dorse Region",
    461: "Map Name", 462: "Area 11 Forest", 463: "Area 11 Shallow Lake",
    464: "Area 34 Cave Parsecs Room", 465: "Area 34 Fire Mountain Cave", 466: "Cellar",
    467: "Forest Metropolis 1st Floor", 468: "Forest Metropolis 1st Floor",
    469: "Forest Metropolis 1st Floor", 470: "Kitchen", 471: "Brewery",
    472: "Forest Metropolis 2nd Floor", 473: "Room", 474: "Room",
    475: "Forest Metropolis 2nd Floor", 476: "Room", 477: "Room",
    478: "Forest Metropolis 3rd Floor", 479: "Elder's Room", 480: "Storeroom",
    481: "Nowem Region", 482: "Nowem Region", 483: "Nowem Region",
    484: "Nowem Region", 485: "Nowem Region", 486: "Area 09D Elf Village",
    487: "Area 09 Valley", 488: "Area 09 Forest", 489: "Sediche Region-Nowem Region",
    490: "Wind Valley", 491: "Wind Valley", 492: "Wind Valley",
    493: "Wind Valley", 494: "Nowem Region", 495: "Map Name (Graveyard of the Elves)",
    496: "Goblin Haven", 497: "Sediche Region", 498: "Sediche Region",
    499: "Sediche Region", 500: "Sediche Region", 501: "Sediche Region",
    502: "Sediche Region", 503: "Goblin Haven",
    504: "Area 22B Goblin Village Square", 505: "Area 22 Mushroom Forest (Upper Path)",
    506: "Area 22 Mushroom Forest (Path)", 507: "Goblin Haven", 508: "Room (Forest Metropolis)",
    509: "Map Name (Outside south gate of Fort Helencia)", 510: "Fort Helencia Anteroom",
    511: "Nowem Region-Tria Region", 512: "Tria Region",
    513: "Fort Helencia Courtyard", 514: "Fort Helencia Accessories",
    515: "Fort Helencia Pharmacy", 516: "Tria Region", 517: "Tria Region",
    518: "Solieu Village", 519: "Tria Region", 520: "Fort Helencia Entrance",
    521: "Fort Helencia Courtyard (Decayed)", 522: "Fort Helencia Anteroom (Decayed)",
    523: "Fort Helencia Entrance", 524: "Fort Helencia Anteroom",
    525: "Fort Helencia Anteroom", 526: "Fort Helencia Passage",
    527: "Fort Helencia Shelter", 528: "Area 03 Plains", 529: "Area 03 Village",
    530: "Area 03 In Front Of Boulder", 531: "Area 03 Boulder Plains",
    532: "Area 03 Boulder Interior", 533: "Fort Helencia Anteroom",
    534: "Fort Helencia Jacks Room", 535: "Area 03 Grapevine", 536: "Dorse Region",
    537: "Dorse Region", 538: "Dorse Region", 539: "Dorse Region",
    540: "0540", 541: "0541", 542: "0542", 543: "0543", 544: "0544",
    545: "Goblin Cemetery", 546: "Goblin Cemetery", 547: "Goblin Cemetery",
    548: "Goblin Cemetery", 549: "Goblin Cemetery", 550: "Goblin Cemetery",
    551: "Goblin Cemetery", 552: "Goblin Cemetery", 553: "Goblin Cemetery",
    554: "Goblin Cemetery", 555: "0555", 556: "0556", 557: "0557",
    558: "0558", 559: "0559", 560: "0560", 561: "0561", 562: "0562",
    563: "0563", 564: "0564", 565: "0565", 566: "0566", 567: "0567",
    568: "0568", 569: "0569",
    570: "Area 12 Goblin Cemetery Room", 571: "Area 12 Goblin Village Square",
    572: "Area 12 Cemetery Main Room", 573: "Area 12 Tunnel", 574: "Area 12 Savanna (No Fort)",
    575: "0575", 576: "Watchman's Room", 577: "Cell", 578: "Garcia",
    579: "Cell", 580: "Cell", 581: "Bran Wal", 582: "Cell", 583: "Cell",
    584: "Sarval", 585: "Bligh", 586: "Howard & Stefan", 587: "Cell",
    588: "Natalie's Room", 589: "Infirmary", 590: "Coliseum Locker Room",
    591: "Radiata Castle 2nd Floor Hall", 592: "Radiata Castle 3rd Floor Hall",
    593: "Radiata Castle 3rd Floor Hall", 594: "Radiata Castle Arena",
    595: "Radiata Castle Outskirts", 596: "Dysett Region", 597: "Dysett Region",
    598: "Gold Dragon Castle", 599: "Gold Dragon Castle",
    600: "City Of White Nights", 601: "Area 36 Desert", 602: "Dysett Region",
    603: "Area 13 Spiral Corridor", 604: "City Of White Nights", 605: "Map Name (Battlefield for Aphelion)",
    606: "Path Of The Spider", 607: "Path Of The Spider", 608: "Path Of The Spider",
    609: "Path Of The Spider", 610: "Path Of The Spider", 611: "Path Of The Spider",
    612: "Path Of The Spider", 613: "Path Of The Spider", 614: "Path Of The Spider",
    615: "Path Of The Spider", 616: "Path Of The Spider", 617: "Path Of The Spider",
    618: "Path Of The Spider", 619: "Path Of The Spider", 620: "Path Of The Spider",
    621: "Path Of The Spider", 622: "Path Of The Spider", 623: "Path Of The Spider",
    624: "Path Of The Spider", 625: "Path Of The Spider", 626: "Path Of The Spider",
    627: "Path Of The Spider", 628: "Path Of The Spider Charnel",
    629: "Path Of The Spider", 630: "Path Of The Spider", 631: "Path Of The Spider",
    632: "Path Of The Spider", 633: "Path Of The Spider", 634: "Path Of The Spider",
    635: "Path Of The Spider", 636: "Path Of The Spider", 637: "Path Of The Spider",
    638: "Path Of The Spider", 639: "Path Of The Spider", 640: "Path Of The Spider",
    641: "Path Of The Spider Hidden Room", 642: "Path Of The Spider Hidden Pass",
    643: "Path Of The Spider", 644: "Map Name (Gold Dragon Castle and City of White Nights)", 645: "0645",
    646: "Dwarf Tunnel", 647: "Dwarf Tunnel", 648: "Dwarf Tunnel",
    649: "Dwarf Tunnel", 650: "Tria Region-Dova Region", 651: "Dwarf Tunnel",
    652: "Dwarf Tunnel", 653: "Dwarf Tunnel", 654: "Dwarf Tunnel",
    655: "Dwarf Tunnel", 656: "Dwarf Tunnel", 657: "Dwarf Tunnel",
    658: "Dwarf Tunnel", 659: "Dwarf Tunnel", 660: "Dwarf Tunnel",
    661: "Dwarf Tunnel", 662: "Dwarf Tunnel", 663: "Dwarf Tunnel",
    664: "Dwarf Tunnel", 665: "Dwarf Tunnel", 666: "Dwarf Tunnel",
    667: "Dwarf Tunnel", 668: "Dwarf Tunnel", 669: "Dwarf Tunnel",
    670: "Dwarf Tunnel", 671: "Dwarf Tunnel", 672: "Dwarf Tunnel",
    673: "Dwarf Tunnel", 674: "Ganz's Home", 675: "0675 (Path of the Spider)",
    676: "0676", 677: "0677", 678: "0678", 679: "0679", 680: "0680",
    681: "0681", 682: "0682", 683: "0683", 684: "0684", 685: "0685",
    686: "0686", 687: "0687", 688: "0688", 689: "0689", 690: "0690",
    691: "0691", 692: "0692", 693: "0693", 694: "0694", 695: "Map Name (Radiata City Yellow Town)",
    696: "Warrior Guild Training Facility",
    697: "Radiata Castle Near Alcon Gate",
    698: "Radiata Castle Near Echidna Gate",
    699: "Radiata Castle Near Heliforde",
    700: "Radiata Castle Near Lupus Gate",
    701: "Sewer 1st Floor (With Water)", 702: "Sewer 1st Floor (No Water)",
    703: "Sewer 2nd Floor (With Water)", 704: "Sewer 2nd Floor (No Water)",
    705: "Map Name (Radiata City dark clouds over Radiata Castle)", 706: "Radiata Lupus Gate Entrance", 707: "Yellow Town of the Sun and Glory",
    708: "Warrior Guild Facade", 709: "Warrior Town Alley", 710: "Theater Vancoor 1st Floor",
    711: "Interview Room", 712: "Toilet", 713: "Theater Vancoor 2nd Floor",
    714: "Training Facility", 715: "The Triton Squad Locker Room",
    716: "The Quarto Squad Locker Room", 717: "Theater Vancoor 3rd Floor",
    718: "The Zweit Squad Locker Room", 719: "Theater Vancoor 4th Floor",
    720: "Chief's Room", 721: "Treasury",
    722: "Theater Vancoor Basement 1st Floor", 723: "Infirmary",
    724: "The Hecton Squad Locker Room", 725: "The Quintom Squad Locker Room",
    726: "Theater Vancoor Basement 2nd Floor", 727: "Cell", 728: "Storeroom",
    729: "Star's Room", 730: "Yellow Town of the Sun and Glory",
    731: "Swords And Silver Coins Inn",
    732: "Swords And Silver Coins Inn 2nd Floor",
    733: "Swords And Silver Coins Inn Room 201",
    734: "Swords And Silver Coins Inn Room 101",
    735: "Swords And Silver Coins Inn Room 203", 736: "Begin Eatery",
    737: "Begin Eatery 2nd Floor", 738: "The Survivor Armory",
    739: "San Patty Accessories", 740: "San Patty Accessories 2nd Floor",
    741: "Lupus Gate Guard Post", 742: "Jarvis's Place",
    743: "Tigers Apartments 1st Floor", 744: "Yuri's Place",
    745: "Vacant House", 746: "Tigers Apartments 2nd Floor",
    747: "Room 102", 748: "Room 103", 749: "Room 202", 750: "Room 203",
    751: "The Survivor Room (Unfinished)", 752: "Radiata Heliforde Gate Entrance",
    753: "Radiata Heliforde Gate Entrance",
    754: "Radiata Echidna Gate Entrance", 755: "0755",
    756: "Distortion Corridor", 757: "Distortion Corridor",
    758: "Distortion Corridor", 759: "Distortion Corridor",
    760: "Distortion Corridor", 761: "Distortion Corridor",
    762: "Distortion Corridor", 763: "Distortion Corridor",
    764: "Distortion Corridor", 765: "Distortion Corridor", 766: "White Town of Stars and Faith",
    767: "Morfinn's Clinic Med Storeroom", 768: "Morfinn's Clinic",
    769: "Morfinn's Clinic Examination Room",
    770: "Morfinn's Clinic 2nd Floor", 771: "Eisenhower Pharmacy",
    772: "Eisenhower Pharmacy Preparation Room",
    773: "The Last Word Book Store",
    774: "The Last Word Book Store Vault",
    775: "Waldo General Store", 776: "Storeroom",
    777: "Peaceful Pony Inn", 778: "Peaceful Pony Inn Diner",
    779: "Map 573", 780: "Peaceful Pony Inn Room 201 (Unfinished)",
    781: "Peaceful Pony Inn Room 202", 782: "Peaceful Pony Inn Room 203",
    783: "Peaceful Pony Inn 2nd Floor", 784: "Heliforde Gate Guard Post",
    785: "Elena & Adina's Place", 786: "Private House",
    787: "Grant's Place", 788: "Edgar Cosmos Place",
    789: "Flora & Synelia's Place", 790: "Rocky's Place",
    791: "Distortion Corridor", 792: "Distortion Corridor",
    793: "Distortion Corridor", 794: "Distortion Corridor",
    795: "Distortion Corridor", 796: "Olacion Order Shrine",
    797: "Olacion Order Chapel", 798: "Anastasia's Place",
    799: "Dwight's Place", 800: "Godwin's Place",
    801: "Fernando's Place", 802: "Anastasia's Room",
    803: "Fernando's Room", 804: "Dwight's Room",
    805: "Godwin's Room", 806: "Vitas's Room",
    807: "Cosmo's Room", 808: "Treasury", 809: "Confessional",
    810: "Olacion Order Mortal Tree Hall",
    811: "Olacion Order Universal Tree Hall", 812: "Confessional",
    813: "Priest Guild Center", 814: "Reception (In Dwight's place)", 815: "Basement (In Dwight's place)",
    816: "Treasury", 817: "Priest Town Alley",
    818: "Distortion Corridor", 819: "Distortion Corridor",
    820: "Distortion Corridor", 821: "Distortion Corridor",
    822: "Distortion Corridor", 823: "Distortion Corridor",
    824: "Distortion Corridor", 825: "Distortion Corridor",
    826: "0826", 827: "Distortion Corridor",
    828: "Map 622", 829: "Cherie's Place", 830: "Cecil's Place",
    831: "Alvin's Place", 832: "Abandoned Building",
    833: "Clive's Place", 834: "Clive's Place",
    835: "Abandoned Building 1st Floor",
    836: "Abandoned Building 2nd Floor",
    837: "Abandoned Building Basement",
    838: "Distortion Corridor", 839: "Distortion Corridor",
    840: "Shrine Of Fray", 841: "Shrine Of Fray",
    842: "Shrine Of Fray", 843: "Shrine Of Fray",
    844: "Shrine Of Fray", 845: "Shrine Of Fray",
    846: "Path To The Beast Pit", 847: "Olacion Order Shrine",
    848: "Shrine Of Fray", 849: "Shrine Of Fray",
    850: "Shrine Of Fray", 851: "Shrine Of Fray",
    852: "Shrine Of Fray", 853: "Shrine Of Fray",
    854: "Shrine Of Fray", 855: "Shrine Of Fray",
    856: "Vareth Magic Institute 2nd Floor",
    857: "Star Tower Research Lab", 858: "Star Tower Research Lab",
    859: "Star Tower Research Lab", 860: "Moon Tower Research Lab",
    861: "Moon Tower Research Lab", 862: "Moon Tower Research Lab",
    863: "Presidents Office", 864: "Observatory",
    865: "Cafeteria", 866: "Infirmary", 867: "Storeroom",
    868: "Elevator", 869: "Star Tower", 870: "Moon Tower",
    871: "Room 201", 872: "Star Tower Interior",
    873: "Moon Tower Interior", 874: "0874",
    875: "Star Tower - President's Office",
    876: "President's Office - Moon Tower",
    877: "Map 671 (Generator room in Vareth)", 878: "Shrine Of Fray", 879: "Shrine Of Fray",
    880: "Dragon Lair Cave", 881: "Dragon Lair Cave",
    882: "Dragon Lair Cave", 883: "Dragon Lair Cave",
    884: "Dragon Lair Cave", 885: "Dragon Lair Cave",
    886: "Vancoor Square", 887: "Map 681",
    888: "Belmont General Store",
    889: "Belmont General Store Bedroom",
    890: "Verontier Armory",
    891: "Verontier Armory Bedroom",
    892: "Verontier Armory Storeroom",
    893: "Walter Sheilas Place", 894: "Daniel's Place",
    895: "Carl's Pub", 896: "Dragon Lair Cave",
    897: "Dragon Lair Cave", 898: "Distortion Corridor",
    899: "Map Name",
    900: "Map Name", 901: "Map Name", 902: "Map Name", 903: "Map Name (Wind dragon room)",
    904: "Map Name (Earth dragon room)", 905: "Map Name (Water dragon room)", 906: "Map Name (Fire dragon room)", 907: "Map Name (Radian room)",
    908: "Dragon Lair Cave", 909: "Dragon Lair Cave", 910: "Dragon Lair Cave",
    911: "Dragon Lair Cave", 912: "Dragon Lair Cave", 913: "Dragon Lair Cave",
    914: "Dragon Lair Cave", 915: "Dragon Lair Cave", 916: "Path to the Sun", 917: "0917",
    918: "Gregory's Place", 919: "Alicia's Place", 920: "Gerald's Place",
    921: "Caesar's Place", 922: "Dragon Lair Cave",
    923: "Space Of Imperium", 924: "Corridor Of Peril",
    925: "Corridor Of Peril", 926: "Beast Pit",
    927: "Dragon Lair Cave", 928: "Dragon Lair Cave",
    929: "Dragon Lair Cave", 930: "Dragon Lair Cave",
    931: "Dragon Lair Cave", 932: "Dragon Lair Cave",
    933: "Dragon Lair Cave", 934: "Dragon Lair Cave",
    935: "Space Of Chaos", 936: "Corridor Of Delusions",
    937: "Corridor Of Delusions", 938: "Map Name", 939: "Map Name (Ethereal Queen battlefield)",
    940: "Dragon Lair Cave", 941: "Dragon Lair Cave",
    942: "Dragon Lair Cave", 943: "Dragon Lair Cave",
    944: "Lyle's Mansion (Unfinished)", 945: "Lyle's Mansion (Exterior)", 946: "Path of Swords and Wisdom", 947: "Map 741",
    948: "Blade Pharmacy", 949: "Blade Pharmacy Vault Room",
    950: "Blade Pharmacy Vault", 951: "Lantana's Place (Unfinished/Debug)",
    952: "Gordon's Place", 953: "Gareth & Rolec's Place",
    954: "David's Place", 955: "Vitas & Miranda's Place",
    956: "Achilles & Eugene's Place", 957: "Aldo's Place",
    958: "Paul's Place", 959: "Bruce's Place", 960: "Jack's Place",
    961: "Dragon Lair Cave", 962: "Dragon Lair Cave",
    963: "Map Name", 964: "Map Name", 965: "Map Name",
    966: "Map Name", 967: "Dragon Lair Cave (Entrance)",
    968: "Dragon Lair Cave (Wind dragon room)", 969: "Dragon Lair Cave (Water dragon room)",
    970: "Dragon Lair Cave (Fire dragon room)", 971: "Dragon Lair Cave (Earth dragon room)",
    972: "Dragon Lair Cave", 973: "0973", 974: "0974",
    975: "0975", 976: "Beast Pit", 977: "0977",
    978: "Alkaico General Store", 979: "Storeroom", 980: "Storeroom (2nd floor storeroom)",
    981: "Club Vampire", 982: "Map 776",
    983: "Club Vampire 2nd Floor", 984: "The Vampire Casino",
    985: "Void Community Basement", 986: "Void Community Vault Room",
    987: "Void Community Office", 988: "Void Community The Abyss",
    989: "Map 783", 990: "Void Community Torture Room",
    991: "Void Community Hall", 992: "Servia's Place",
    993: "Bedroom", 994: "Ortoroz & Silvia's Place",
    995: "Dan's Place", 996: "0996", 997: "0997", 998: "0998", 999: "0999",
    1000: "1000", 1001: "1001", 1002: "1002", 1003: "1003", 1004: "1004",
    1005: "1005", 1006: "Faid General Store",
    1007: "Faid General Store Vault Room", 1008: "Vault",
    1009: "Private House",
    1010: "Red Lotus Metropolis Party Room",
    1011: "Room", 1012: "Room", 1013: "Room (Red Lotus Metropolis lounge)",
    1014: "Red Lotus Metropolis",
    1015: "Red Lotus Metropolis 2nd Floor",
    1016: "Room 101", 1017: "Room 102",
    1018: "Room 201", 1019: "Room 202",
    1020: "Gepald Apartments 1st Floor",
    1021: "Gepald Apartments 2nd Floor",
    1022: "Startis & Butch's Place", 1023: "Startis & Butch's Place",
    1024: "Zeranium's Place", 1025: "Black Town of Night and Lust",
    1026: "Bandit Town Alley", 1027: "1027", 1028: "1028",
    1029: "1029", 1030: "1030", 1031: "1031",
    1032: "1032", 1033: "1033", 1034: "1034", 1035: "1035",
    1036: "Nocturne's Place", 1037: "Nocturne's Place",
    1038: "Beast Pit", 1039: "1039", 1040: "1040",
    1041: "1041", 1042: "1042", 1043: "1043",
    1044: "1044", 1045: "1045", 1046: "Beast Pit",
    1047: "1047", 1048: "1048", 1049: "1049",
    1050: "1050", 1051: "1051", 1052: "1052",
    1053: "1053", 1054: "1054", 1055: "1055",
    1056: "1056", 1057: "1057", 1058: "1058",
    1059: "1059", 1060: "1060", 1061: "1061",
    1062: "1062", 1063: "1063", 1064: "1064",
    1065: "1065", 1066: "Beast Pit", 1067: "1067",
    1068: "Mysterious Creatures Inn",
    1069: "Room 201", 1070: "Room 202", 1071: "Room 203",
    1072: "Mysterious Creatures Inn 2nd Floor",
    1073: "1073", 1074: "1074",
    1075: "Levante General Store", 1076: "Storeroom",
    1077: "Dead End Armory", 1078: "1078", 1079: "1079",
    1080: "1080", 1081: "1081", 1082: "1082",
    1083: "1083", 1084: "1084", 1085: "1085",
    1086: "Chic Records", 1087: "1087",
    1088: "Beast Pit", 1089: "1089",
    1090: "1090", 1091: "1091", 1092: "1092",
    1093: "1093", 1094: "1094", 1095: "1095",
    1096: "1096", 1097: "1097", 1098: "1098", 1099: "1099",
    1100: "1100", 1101: "1101", 1102: "1102", 1103: "1103",
    1104: "1104", 1105: "1105", 1106: "Beast Pit",
    1107: "1107", 1108: "Flau's Place", 1109: "Flau's Place",
    1110: "Rynka's Place", 1111: "Rynka's Place",
    1112: "1112", 1113: "1113", 1114: "1114", 1115: "1115",
    1116: "1116", 1117: "1117", 1118: "1118", 1119: "1119",
    1120: "1120", 1121: "1121", 1122: "1122",
    1123: "1123", 1124: "1124", 1125: "1125",
    1126: "Beast Pit", 1127: "1127",
    1128: "Lunbar's Place", 1129: "Solo's Place",
    1130: "Eon's Place", 1131: "Jared's Place",
    1132: "1132", 1133: "1133", 1134: "1134",
    1135: "1135", 1136: "1136", 1137: "1137",
    1138: "1138", 1139: "1139", 1140: "1140",
    1141: "1141", 1142: "1142", 1143: "1143",
    1144: "Blue Town of Water and Wisdom", 1145: "Mage Town Alley", 1146: "Blue Town of Water and Wisdom",
    1147: "Outside Mage Guild",
    1148: "Black Rose General Store", 1149: "Storeroom",
    1150: "Ok Hand Accessories", 1151: "Vault",
    1152: "Teagle Apartments 1st Floor",
    1153: "Room 101", 1154: "Room 102", 1155: "Room 103",
    1156: "Teagle Apartments 2nd Floor",
    1157: "Room 201", 1158: "Room 202", 1159: "Room 203",
    1160: "Teagle Apartments 3rd Floor",
    1161: "Room 301", 1162: "Room 302", 1163: "Room 303",
    1164: "Leopearl Apartments 1st Floor",
    1165: "Room 101", 1166: "Room 102", 1167: "Room 103",
    1168: "Leopearl Apartments 2nd Floor",
    1169: "Room 201", 1170: "Room 202", 1171: "Room 203 (Cornelia's place)",
    1172: "Leopearl Apartments 3rd Floor",
    1173: "Room 301", 1174: "Room 302", 1175: "Room 303",
    1176: "Orso Apartments 1st Floor",
    1177: "Room 103", 1178: "Room 102", 1179: "Room 101",
    1180: "1180", 1181: "Room 203", 1182: "Room 202",
    1183: "Room 201", 1184: "Curtis's Place",
    1185: "Cache Apartments 1st Floor",
    1186: "Room 101", 1187: "Room 102", 1188: "Room 103",
    1189: "Cache Apartments 2nd Floor",
    1190: "Room 203", 1191: "Room 202", 1192: "1192",
    1193: "Vareth Magic Institute",
    1194: "Echidna Gate Guard Post",
    1195: "1195", 1196: "Map 990 (Title Screen)", 1197: "Installation Center",
    1198: "1198", 1199: "1199", 1200: "Event Checking Room",
    1201: "Map 995 (Used for Model Viewer)", 1202: "Map 996",
    1203: "Map 997", 1204: "Map 998", 1205: "Map 999",
}

# Character data packs (1206–1510)
CHARACTER_LABELS = {
    1206: "Bank Base", 1207: "Jack", 1208: "Ganz", 1209: "Ridley", 1210: "Rynka",
    1211: "Flau", 1212: "Star", 1213: "Sebastian", 1214: "Genius", 1215: "Rocky",
    1216: "Gawain", 1217: "Heavy Guardsman", 1218: "Elwen", 1219: "Gerald",
    1220: "Caesar", 1221: "Alicia", 1222: "Dennis", 1223: "Gareth",
    1224: "Gregory", 1225: "Walter", 1226: "Jarvis", 1227: "Light Guardsman",
    1228: "Aldo", 1229: "Gordon", 1230: "Bruce", 1231: "David",
    1232: "Conrad", 1233: "Rolec", 1234: "Daniel", 1235: "Carlos",
    1236: "Gene", 1237: "Light Guardsman", 1238: "Thanos", 1239: "Curtis",
    1240: "Cecil", 1241: "Morgan", 1242: "Felix", 1243: "Jill",
    1244: "Ursula", 1245: "Derek", 1246: "Christoph", 1247: "Claudia",
    1248: "Ardoph", 1249: "Dimitri", 1250: "Aidan", 1251: "Cornelia",
    1252: "Faraus", 1253: "Marietta", 1254: "Ernest", 1255: "Franklin",
    1256: "Johan", 1257: "Roche", 1258: "Light Guardsman", 1259: "Kain",
    1260: "Fernando", 1261: "Anastasia", 1262: "Dwight", 1263: "Godwin",
    1264: "Achilles", 1265: "Flora", 1266: "Elena", 1267: "Alvin",
    1268: "Vitas", 1269: "Cosmo", 1270: "Grant", 1271: "Adina",
    1272: "Miranda", 1273: "Edgar", 1274: "Clive", 1275: "Lulu",
    1276: "Eugene", 1277: "Nyx", 1278: "Ortoroz", 1279: "Sonata",
    1280: "Iris", 1281: "Nocturne", 1282: "Herz", 1283: "Alba",
    1284: "Lily", 1285: "Jared", 1286: "Pinky", 1287: "Interlude",
    1288: "Solo", 1289: "Joaquel", 1290: "Eon", 1291: "Elmo",
    1292: "Jiorus", 1293: "Sarasenia", 1294: "Belflower", 1295: "Jasne",
    1296: "Larks", 1297: "Sakurazaki", 1298: "Junzaburo", 1299: "Natalie",
    1300: "Nina", 1301: "Charlie", 1302: "Leonard", 1303: "Light Guardsman",
    1304: "Heavy Guardsman", 1305: "Raymond", 1306: "Al", 1307: "Margaret",
    1308: "Zion", 1309: "Paul", 1310: "Toma", 1311: "Torenia",
    1312: "Testa", 1313: "Nuse", 1314: "Jorn", 1315: "Barbena",
    1316: "Giske", 1317: "Yuri", 1318: "Warc", 1319: "Robin",
    1320: "Sheila", 1321: "Jasmine", 1322: "Camuse", 1323: "Lantana",
    1324: "Lyle", 1325: "Rose", 1326: "Josef", 1327: "Virginia",
    1328: "Morfinn", 1329: "Bligh", 1330: "Freija", 1331: "Nask",
    1332: "Cherie", 1333: "Zeke", 1334: "Dan", 1335: "Servia",
    1336: "Lunbar", 1337: "Sonia", 1338: "Startis", 1339: "Brood",
    1340: "Garbella", 1341: "Silvia", 1342: "Thyme", 1343: "Elef",
    1344: "Ryan", 1345: "Hip", 1346: "Nick", 1347: "Kira",
    1348: "Rabi", 1349: "Golye", 1350: "Butch", 1351: "Sarval",
    1352: "Sunset", 1353: "Sora", 1354: "Keaton", 1355: "Tarkin",
    1356: "Gonber", 1357: "Leban", 1358: "Mook", 1359: "Wal",
    1360: "Wyze", 1361: "Zeranium", 1362: "156", 1363: "Pommelie",
    1364: "Saron", 1365: "Cepheid", 1366: "Baade", 1367: "Quasar",
    1368: "Aphelion", 1369: "Gonovitch", 1370: "Albert", 1371: "Vladimir",
    1372: "Yevgeni", 1373: "Oleg", 1374: "Grigory", 1375: "Brockle",
    1376: "Dyvad", 1377: "Gehrmann", 1378: "Sergei", 1379: "Naom",
    1380: "Aegenhart", 1381: "Marke", 1382: "Donovitch", 1383: "Zane",
    1384: "Hap", 1385: "Gil", 1386: "Shin", 1387: "Fan",
    1388: "Row", 1389: "Pitt", 1390: "Few", 1391: "Alan",
    1392: "Keane", 1393: "Nogueira", 1394: "Clarence", 1395: "Serva",
    1396: "Hyann", 1397: "Chatt", 1398: "Zida", 1399: "Franz",
    1400: "Romaria", 1401: "Marsha", 1402: "Lufa", 1403: "Coco",
    1404: "Martinez", 1405: "Santos", 1406: "Rika", 1407: "Mikey",
    1408: "Gob", 1409: "Lin", 1410: "Brie", 1411: "Gonn",
    1412: "Golly", 1413: "Gobrey", 1414: "Den", 1415: "Ben",
    1416: "Aesop", 1417: "Monki", 1418: "Gabe", 1419: "Mason",
    1420: "Goo", 1421: "Donkey", 1422: "Ricky", 1423: "Drew",
    1424: "Gruel", 1425: "Doppio", 1426: "Pietro", 1427: "Jan",
    1428: "Marco", 1429: "Niko", 1430: "Danny", 1431: "Dominic",
    1432: "Bosso", 1433: "Georgio", 1434: "Luka", 1435: "Sonny",
    1436: "Giovanni", 1437: "Polpo", 1438: "Jj", 1439: "Leona",
    1440: "Leann", 1441: "Ray C Ross", 1442: "Pinta", 1443: "Buta",
    1444: "Valkyrie", 1445: "Lezard", 1446: "Radian", 1447: "Ethereal Queen",
    1448: "Cairn", 1449: "Kelvin", 1450: "Gabriel Celesta", 1451: "Not Implemented 1",
    1452: "Not Implemented 2", 1453: "Galvados", 1454: "Not Implemented 3", 1455: "Not Implemented 4",
    1456: "Not Implemented 5", 1457: "Not Implemented 6", 1458: "Not Implemented 7", 1459: "Drago",
    1460: "Bull", 1461: "Not Implemented 8", 1462: "Not Implemented 9", 1463: "Not Implemented 10",
    1464: "Not Implemented 11", 1465: "Library", 1466: "Phonograph", 1467: "Jack Bookshelf",
    1468: "Cross", 1469: "Stein", 1470: "Blackjack", 1471: "Event Watcher",
    1472: "Parsec", 1473: "Light Guardsman", 1474: "Light Guardsman",
    1475: "Light Guardsman", 1476: "Heavy Guardsman", 1477: "Heavy Guardsman",
    1478: "Heavy Guardsman", 1479: "Heavy Guardsman", 1480: "Heavy Guardsman",
    1481: "Heavy Guardsman", 1482: "Heavy Guardsman", 1483: "Heavy Guardsman",
    1484: "Heavy Guardsman", 1485: "Cody", 1486: "Adele", 1487: "Howard",
    1488: "Ravil", 1489: "Astor", 1490: "Maddock", 1491: "Synelia",
    1492: "Tony", 1493: "Patrick", 1494: "Putt", 1495: "Reynos",
    1496: "Gobblehope Ix", 1497: "Nalshay", 1498: "Sayna", 1499: "Bran",
    1500: "Stefan", 1501: "Mint", 1502: "Daria", 1503: "Yack",
    1504: "Lauren", 1505: "Theresa", 1506: "Garcia", 1507: "Dynas",
    1508: "Epoch", 1509: "Roy", 1510: "Louis",
}

# Monster data packs (1511–1687)
MONSTER_LABELS = {
    1511: "Bank Base", 1512: "Smilodon M", 1513: "Smilodon F", 1514: "Colossalizard",
    1515: "Tusky Mammoth", 1516: "Tsuchinoko", 1517: "Big Jaws", 1518: "King Serpent",
    1519: "Bitty Ant", 1520: "Giga Ant", 1521: "Flame Ant", 1522: "Killer Queen",
    1523: "Deathclover", 1524: "Poisonous Lizard", 1525: "Ice Lizard",
    1526: "Blauniebel", 1527: "Shell Lizard", 1528: "Ivory Goat", 1529: "Crocogator",
    1530: "Ripple Bat", 1531: "Carnivorat", 1532: "Pararat", 1533: "Militarat",
    1534: "Hunterwolf", 1535: "Rooster", 1536: "Hen", 1537: "Chick", 1538: "Ogre",
    1539: "Black Cat", 1540: "White Cat", 1541: "Owl", 1542: "Red Crow",
    1543: "Blue Crow", 1544: "Leaping Crow", 1545: "Pigeon Crow", 1546: "Crow",
    1547: "Sparrow", 1548: "Birdcage Insect", 1549: "Bitty Hopper",
    1550: "Giga Hopper", 1551: "Little Oily", 1552: "Oily Bug", 1553: "Rolly Polly",
    1554: "Speckled Bug", 1555: "Flame Lizard", 1556: "Cute Dog", 1557: "Tall Beast",
    1558: "Twin Horn", 1559: "Mount Tortoise", 1560: "Metal Tortoise",
    1561: "Greek Tortoise", 1562: "Tawny Rat", 1563: "Goblin Elefant",
    1564: "Bubble Frogger", 1565: "Mist Frogger", 1566: "Fennec Fox", 1567: "Buta",
    1568: "Spray Snake", 1569: "Dagol Tortoise", 1570: "Whip Turtle",
    1571: "Crunchy Shell", 1572: "Female Knight", 1573: "Male Knight",
    1574: "Light Guardsman", 1575: "Heavy Guardsman", 1576: "Therosaurus",
    1577: "Mountain Goat", 1578: "Mountain Goat", 1579: "Mountain Goat", 1580: "Wolf",
    1581: "Glyptodon", 1582: "Alesnoi", 1583: "Robo Dwarf B", 1584: "Robo Dwarf R",
    1585: "Zerotone", 1586: "Gold Box Turtle", 1587: "Lemon Box Turtle", 1588: "Fire",
    1589: "Quetzalcoatl", 1590: "Round Knight", 1591: "Round Knight",
    1592: "Mountain Goat", 1593: "Mountain Goat", 1594: "Rockdigger",
    1595: "Bone Goblin", 1596: "RoboStar", 1597: "Melissa", 1598: "Melissa II",
    1599: "Apprentice", 1600: "Archdemon", 1601: "Mud Pawn", 1602: "Mud Fighter",
    1603: "Mud Mage", 1604: "Hemud", 1605: "Shemud", 1606: "Mud Bone",
    1607: "Mud Dile", 1608: "Bison", 1609: "Bison", 1610: "Bison", 1611: "Bison",
    1612: "Bison", 1613: "Bison", 1614: "Bison", 1615: "Bison", 1616: "Mud Ponbabar",
    1617: "Tree Frog", 1618: "Common Toad", 1619: "Gecko", 1620: "Flat-Tail Gecko",
    1621: "Skyfish", 1622: "Mole", 1623: "Char", 1624: "Ghar", 1625: "Goldfish",
    1626: "Water Bubblefish", 1627: "Bug-Eye Goldfish", 1628: "Butterflyfish",
    1629: "Bichir", 1630: "Water Strider", 1631: "Beetle", 1632: "Spider",
    1633: "Stag Beetle", 1634: "Gold Beetle", 1635: "Butterfly", 1636: "Dragonfly",
    1637: "Goblin Elefant", 1638: "Trap Octopus", 1639: "Green Orc", 1640: "Green Orc",
    1641: "Green Orc", 1642: "Blood Orc", 1643: "Blood Orc", 1644: "Hellraiser",
    1645: "Grim Reaper", 1646: "Darksoul", 1647: "Bubu", 1648: "Copperfish",
    1649: "Subordinate Mage", 1650: "Treeman", 1651: "Dwarf", 1652: "Light Elf",
    1653: "Dark Elf", 1654: "Green Goblin", 1655: "Black Goblin", 1656: "Living Totem",
    1657: "Fire Cell", 1658: "Aqua Cell", 1659: "Wind Cell", 1660: "Earth Cell",
    1661: "Dark Cell", 1662: "Flash Cell", 1663: "Crystal Ball", 1664: "Pointura",
    1665: "Iceburg", 1666: "Skullhead", 1667: "Burglar", 1668: "Trent",
    1669: "Skypulsar", 1670: "Gobpakken", 1671: "Black Tiger", 1672: "Whirlwind",
    1673: "Phantom", 1674: "Matango", 1675: "Holy Quetzal", 1676: "Willow",
    1677: "Hollywoody", 1678: "Flash Monkey", 1679: "Thundercorn", 1680: "Crystaria",
    1681: "Shrine Knight", 1682: "Wind Cell", 1683: "Earth Cell", 1684: "Earth Cell",
    1685: "Dark Cell", 1686: "Flash Cell", 1687: "1176",
}

# Prop data packs (1688–1938)
PROP_LABELS = {
    1688: 'Bank Base', 1689: 'Bottle', 1690: 'Fancy Book', 1691: 'Boar', 1692: '2004', 1693: '2005',
    1694: 'Goblin Tent', 1695: '2007', 1696: '2008', 1697: 'Radiata Castle Props', 1698: '2010', 1699: 'Dwarf Building Pieces',
    1700: 'Rusty Dwarf Village Pieces', 1701: 'Gold Plade Dwarf Village Pieces', 1702: 'Quartz Tile', 1703: 'Normal Book', 1704: 'Rune Tile', 1705: '2017',
    1706: '2018', 1707: '2019', 1708: '2020', 1709: '2021', 1710: '2022', 1711: '2023',
    1712: '2024', 1713: '2025', 1714: '2026', 1715: '2027', 1716: '2028', 1717: '2029',
    1718: '2030', 1719: '2031', 1720: '2032', 1721: '2033', 1722: '2034', 1723: '2035',
    1724: '2036', 1725: '2037', 1726: '2038', 1727: '2039', 1728: '2040', 1729: '2041',
    1730: '2042', 1731: '2043', 1732: '2044', 1733: '2045', 1734: '2046', 1735: '2047',
    1736: '2048', 1737: '2049', 1738: '2050', 1739: '2051', 1740: '2052', 1741: '2053',
    1742: '2054', 1743: '2055', 1744: '2056', 1745: '2057', 1746: '2058', 1747: '2059',
    1748: '2060', 1749: '2061', 1750: '2062', 1751: '2063', 1752: '2064', 1753: '2065',
    1754: '2066', 1755: '2067', 1756: '2068', 1757: '2069', 1758: '2070', 1759: '2071',
    1760: '2072', 1761: '2073', 1762: '2074', 1763: '2075', 1764: '2076', 1765: '2077',
    1766: '2078', 1767: '2079', 1768: '2080', 1769: '2081', 1770: '2082', 1771: '2083',
    1772: '2084', 1773: '2085', 1774: '2086', 1775: '2087', 1776: '2088', 1777: '2089',
    1778: '2090', 1779: '2091', 1780: '2092', 1781: '2093', 1782: '2094', 1783: '2095',
    1784: 'Handmade Tunic', 1785: '2097', 1786: '2098', 1787: '2099', 1788: '2100',
    1789: '2101', 1790: '2102', 1791: '2103', 1792: '2104', 1793: '2105', 1794: '2106',
    1795: '2107', 1796: '2108', 1797: '2109', 1798: '2110', 1799: '2111', 1800: '2112',
    1801: '2113', 1802: '2114', 1803: '2115', 1804: '2116', 1805: '2117', 1806: '2118',
    1807: '2119', 1808: '2120', 1809: '2121', 1810: '2122', 1811: '2123', 1812: '2124',
    1813: '2125', 1814: '2126', 1815: '2127', 1816: '2128', 1817: '2129', 1818: '2130',
    1819: '2131', 1820: '2132', 1821: '2133', 1822: '2134', 1823: '2135', 1824: '2136',
    1825: '2137', 1826: '2138', 1827: '2139', 1828: '2140', 1829: '2141', 1830: '2142',
    1831: '2143', 1832: '2144', 1833: '2145', 1834: '2146', 1835: '2147', 1836: '2148',
    1837: '2149', 1838: '2150', 1839: '2151', 1840: '2152', 1841: '2153', 1842: '2154',
    1843: '2155', 1844: '2156', 1845: '2157', 1846: 'Iron Edge', 1847: 'Steel Blade',
    1848: 'Knight Edge', 1849: 'Glory Edge', 1850: '2162', 1851: 'Jinn', 1852: '2164',
    1853: 'Kotetsu', 1854: 'Basilisktos', 1855: 'Evil Blade', 1856: 'Hatred Edge',
    1857: 'Phantom Edge', 1858: '2170', 1859: 'Flame Blade', 1860: 'Aqua Blade',
    1861: '2173', 1862: 'Air Blade', 1863: '2175', 1864: '2176', 1865: 'Storm Bringer',
    1866: 'Iron Sword', 1867: 'Steel Saber', 1868: 'Knight Saber', 1869: 'Glory Sword',
    1870: '2182', 1871: 'Falvern', 1872: 'Efreet', 1873: 'Muramasa', 1874: 'Bizenosafune',
    1875: 'Rune Saber', 1876: '2188', 1877: '2189', 1878: 'Bind Saber', 1879: 'Heat Saber',
    1880: '2192', 1881: '2193', 1882: 'Blaze Saber', 1883: 'Grand Saber', 1884: 'Venom Sword',
    1885: '2197', 1886: 'Fake Gram', 1887: 'Iron Axe', 1888: 'Steel Axe', 1889: 'Knight Axe',
    1890: 'Glory Sword', 1891: '2203', 1892: '2204', 1893: '2205', 1894: '2206', 1895: '2207',
    1896: '2208', 1897: '2209', 1898: '2210', 1899: '2211', 1900: '2212', 1901: '2213',
    1902: '2214', 1903: '2215', 1904: '2216', 1905: '2217', 1906: '2218', 1907: '2219',
    1908: '2220', 1909: '2221', 1910: '2222', 1911: '2223', 1912: '2224', 1913: '2225',
    1914: '2226', 1915: '2227', 1916: '2228', 1917: '2229', 1918: '2230', 1919: '2231',
    1920: '2232', 1921: '2233', 1922: '2234', 1923: '2235', 1924: '2236', 1925: '2237',
    1926: '2238', 1927: '2239', 1928: '2240', 1929: '2241', 1930: '2242', 1931: '2243',
    1932: '2244', 1933: '2245', 1934: '2246', 1935: '2247', 1936: '2248', 1937: '2249',
    1938: '2250',
}

# Equipment data packs (1939–2125)
EQUIP_LABELS = {
    1939: "Bank Base", 1940: "Iron Edge", 1941: "Steel Blade", 1942: "Knight Edge", 1943: "Glory Edge", 1944: "Avcoor",
    1945: "Jinn", 1946: "Murasame", 1947: "Kotetsu", 1948: "Basilisktos", 1949: "Evil Blade",
    1950: "Hatred Edge", 1951: "Phantom Edge", 1952: "Spark Edge", 1953: "Flame Blade", 1954: "Aqua Blade",
    1955: "Icicle Edge", 1956: "Air Blade", 1957: "Breeze Edge", 1958: "Lightning Edge", 1959: "Storm Bringer",
    1960: "Iron Edge", 1961: "Steal Saber", 1962: "Knight Saber", 1963: "Glory Sword", 1964: "Holy Sword",
    1965: "Falvern", 1966: "Efreet", 1967: "Bizenosafune", 1968: "Muramasa", 1969: "Rune Saber", 1970: "Curse Sword",
    1971: "Brain Breaker", 1972: "Bind Saber", 1973: "Heat Saber", 1974: "Flame Sword", 1975: "Lævateinn",
    1976: "Blaze Saber", 1977: "Grand Saber", 1978: "Venom Sword", 1979: "Cyclone Sword", 1980: "Fake Gram", 1981: "Iron Axe",
    1982: "Steel Axe", 1983: "Knight Axe", 1984: "Glory Axe", 1985: "Ancient Axe", 1986: "Behemoth", 1987: "Death Scythe", 1988: "Hard Chopper",
    1989: "Bind Smasher", 1990: "Confuse Axe", 1991: "Fall Smasher", 1992: "Mist Axe", 1993: "Flame Axe", 1994: "Spark Chopper", 1995: "Aqua Chopper",
    1996: "Icicle Axe", 1997: "Mad Axe", 1998: "Rock Axe", 1999: "Grand Smasher", 2000: "Earch Chopper",
    2001: "Iron Spear", 2002: "Steel Pike", 2003: "Knight Spear", 2004: "Paradigm", 2005: "Leviathan",
    2006: "Gungnir", 2007: "Medusa Spear", 2008: "Curse Lance", 2009: "Brain Shooter", 2010: "Binding Spear",
    2011: "Duster Pike", 2012: "Mad Spear", 2013: "Grand Pike", 2014: "Water Pike", 2015: "Aqua Spear",
    2016: "Icicle Spear", 2017: "Unidentified", 2018: "Wind Spear", 2019: "Brionac", 2020: "Oratorio",
    2021: "Requiem", 2022: "Sylph Edge", 2023: "Psycho Edge", 2024: "Floating Sword", 2025: "Vettea",
    2026: "Dunvera", 2027: "Vaise", 2028: "Arabum", 2029: "President Blade", 2030: "Toadstool Blade",
    2031: "Broken Sword", 2032: "Ganz Sword", 2033: "Bloody Grip", 2034: "Fathmil", 2035: "Damascus Blade",
    2036: "E. Toadstool Sword", 2037: "Love Me True", 2038: "Blaze Axe", 2039: "Heavy Rain", 2040: "Bear Smasher",
    2041: "Toadstool Axe", 2042: "Unidentified", 2043: "Titan Pike", 2044: "Storm Spear", 2045: "Cracked Spear",
    2046: "Toadstool Lance", 2047: "Abyss", 2048: "Ares Salute", 2049: "Adventia", 2050: "Entier", 2051: "Aldore",
    2052: "Curozide", 2053: "Windmill", 2054: "Arshaja", 2055: "Wellness", 2056: "Atmis", 2057: "Vipole",
    2058: "Naruth", 2059: "Vatirork", 2060: "Neredoe", 2061: "Dark Candle", 2062: "Asteka",
    2063: "Vathao", 2064: "Agroth", 2065: "Villhe", 2066: "Anviteo", 2067: "Suolo",
    2068: "Wanchu", 2069: "Gigantic Hammer", 2070: "Flying Foot", 2071: "Mythril Hammer", 2072: "Ore Hammer",
    2073: "Bloody Hammer", 2074: "Iron Hammer", 2075: "Aron", 2076: "Esthia", 2077: "Raven Claw",
    2078: "Answerer", 2079: "Steel Dagger", 2080: "Butterfly Knife", 2081: "Iron Knife", 2082: "Kogitsunemaru",
    2083: "Heat Dagger", 2084: "Morningstar", 2085: "Head Basher", 2086: "Earth Crusher", 2087: "Bronze Crusher", 2088: "Symphonia", 2089: "Whip",
    2090: "Predator Claw", 2091: "Shovel Claw", 2092: "Chupa Claw", 2093: "Farmer's Hoe", 2094: "Spade", 2095: "Crossbow",
    2096: "Truncheon", 2097: "Halberd", 2098: "2098 (Unknown)", 2099: "Tamtam Club", 2100: "Ladle",
    2101: "Spatula", 2102: "Justice Ruling", 2103: "Winner Ruling", 2104: "Tobacco Pipe", 2105: "Bottle",
    2106: "Guiron Tree", 2107: "King Chain", 2108: "Walking Stick", 2109: "Metal Pipe", 2110: "Fly Swatter",
    2111: "Toadstool Bazooka", 2112: "Slingshot", 2113: "TamTam Slingshot", 2114: "Frying Pan", 2115: "Bokken",
    2116: "Zengen", 2117: "Ancient Magic Book", 2118: "Iron Gauntlet", 2119: "Vagabond's Guitar", 2120: "Knight Axe",
    2121: "Toadstool Lance", 2122: "Garden Fork", 2123: "Umbrella", 2124: "Holy Sword Gram", 2125: "Arbitrator",
}

# VFX data packs (2126–2425)
VFX_LABELS = {
    2126: "Bank Base", 2127: "4001", 2128: "Shangri La", 2129: "4003", 2130: "4004", 2131: "Map998",
    2132: "4006", 2133: "4007", 2134: "4008", 2135: "4009", 2136: "4010", 2137: "4011", 2138: "4012",
    2139: "4013", 2140: "4014", 2141: "4015", 2142: "4016", 2143: "4017", 2144: "4018", 2145: "4019",
    2146: "4020", 2147: "4021", 2148: "4022", 2149: "4023", 2150: "4024", 2151: "4025", 2152: "4026",
    2153: "4027", 2154: "4028", 2155: "4029", 2156: "4030", 2157: "4031", 2158: "4032", 2159: "4033",
    2160: "4034", 2161: "4035", 2162: "4036", 2163: "4037", 2164: "4038", 2165: "4039", 2166: "4040",
    2167: "4041", 2168: "4042", 2169: "4043", 2170: "4044", 2171: "4045", 2172: "4046", 2173: "4047",
    2174: "4048", 2175: "4049", 2176: "4050", 2177: "4051", 2178: "4052", 2179: "4053", 2180: "4054",
    2181: "4055", 2182: "4056", 2183: "4057", 2184: "4058", 2185: "4059", 2186: "4060", 2187: "4061",
    2188: "4062", 2189: "4063", 2190: "4064", 2191: "4065", 2192: "4066", 2193: "4067", 2194: "4068",
    2195: "4069", 2196: "4070", 2197: "4071", 2198: "4072", 2199: "4073", 2200: "4074", 2201: "4075",
    2202: "4076", 2203: "4077", 2204: "4078", 2205: "4079", 2206: "Storeroom", 2207: "4081", 2208: "4082",
    2209: "4083", 2210: "4084", 2211: "4085", 2212: "4086", 2213: "4087", 2214: "4088", 2215: "4089",
    2216: "4090", 2217: "4091", 2218: "4092", 2219: "4093", 2220: "4094", 2221: "4095", 2222: "4096",
    2223: "4097", 2224: "4098", 2225: "4099", 2226: "4100", 2227: "4101", 2228: "4102", 2229: "4103",
    2230: "4104", 2231: "4105", 2232: "4106", 2233: "4107", 2234: "4108", 2235: "4109", 2236: "4110",
    2237: "4111", 2238: "4112", 2239: "4113", 2240: "4114", 2241: "4115", 2242: "4116", 2243: "4117",
    2244: "4118", 2245: "4119", 2246: "4120", 2247: "4121", 2248: "4122", 2249: "4123", 2250: "4124",
    2251: "4125", 2252: "4126", 2253: "4127", 2254: "4128", 2255: "4129", 2256: "4130", 2257: "4131",
    2258: "4132", 2259: "4133", 2260: "4134", 2261: "Tigers Apartments 2nd Floor", 2262: "4136",
    2263: "4137", 2264: "4138", 2265: "4139", 2266: "Alkaico General Store", 2267: "4141", 2268: "4142",
    2269: "4143", 2270: "4144", 2271: "4145", 2272: "4146", 2273: "4147", 2274: "4148", 2275: "4149",
    2276: "4150", 2277: "4151", 2278: "4152", 2279: "4153", 2280: "4154", 2281: "4155", 2282: "4156",
    2283: "4157", 2284: "4158", 2285: "4159", 2286: "Storeroom", 2287: "4161", 2288: "4162", 2289: "4163",
    2290: "4164", 2291: "4165", 2292: "4166", 2293: "4167", 2294: "4168", 2295: "4169", 2296: "4170",
    2297: "4171", 2298: "4172", 2299: "Borgandiazo", 2300: "4174", 2301: "4175", 2302: "4176", 2303: "4177",
    2304: "4178", 2305: "4179", 2306: "4180", 2307: "4181", 2308: "4182", 2309: "4183", 2310: "4184",
    2311: "4185", 2312: "4186", 2313: "4187", 2314: "4188", 2315: "4189", 2316: "4190", 2317: "4191",
    2318: "4192", 2319: "4193", 2320: "4194", 2321: "4195", 2322: "4196", 2323: "Radiata Castle Basement 1st Fl",
    2324: "4198", 2325: "4199", 2326: "4200", 2327: "4201", 2328: "4202", 2329: "4203", 2330: "Borgandiazo",
    2331: "4205", 2332: "4206", 2333: "4207", 2334: "Borgandiazo", 2335: "4209", 2336: "4210", 2337: "4211",
    2338: "4212", 2339: "4213", 2340: "4214", 2341: "4215", 2342: "4216", 2343: "Radiata Castle Basement 1st Fl",
    2344: "4218", 2345: "4219", 2346: "4220", 2347: "4221", 2348: "4222", 2349: "4223", 2350: "4224",
    2351: "4225", 2352: "4226", 2353: "4227", 2354: "4228", 2355: "4229", 2356: "4230", 2357: "4231",
    2358: "4232", 2359: "4233", 2360: "4234", 2361: "4235", 2362: "Sediche Region", 2363: "4237", 2364: "4238",
    2365: "4239", 2366: "4240", 2367: "4241", 2368: "4242", 2369: "4243", 2370: "4244", 2371: "4245",
    2372: "4246", 2373: "4247", 2374: "4248", 2375: "4249", 2376: "4250", 2377: "4251", 2378: "4252",
    2379: "4253", 2380: "4254", 2381: "4255", 2382: "4256", 2383: "4257", 2384: "4258", 2385: "4259",
    2386: "4260", 2387: "4261", 2388: "4262", 2389: "4263", 2390: "4264", 2391: "4265", 2392: "4266",
    2393: "4267", 2394: "4268", 2395: "4269", 2396: "4270", 2397: "4271", 2398: "4272", 2399: "4273",
    2400: "4274", 2401: "4275", 2402: "4276", 2403: "4277", 2404: "4278", 2405: "4279", 2406: "4280",
    2407: "4281", 2408: "4282", 2409: "4283", 2410: "4284", 2411: "4285", 2412: "4286", 2413: "4287",
    2414: "4288", 2415: "4289", 2416: "4290", 2417: "4291", 2418: "4292", 2419: "4293", 2420: "4294",
    2421: "4295", 2422: "4296", 2423: "4297", 2424: "4298", 2425: "4299",
}

# Scene group setups (2426–3425)
SCENE_GROUP_LABELS = {
    2426: "Shared Scene Setup 2426", 2427: "Shared Scene Setup 2427",
    2428: "Shared Scene Setup 2428", 2429: "Shared Scene Setup 2429",
    2430: "Shared Scene Setup 2430", 2431: "Shared Scene Setup 2431",
    2432: "Shared Scene Setup 2432", 2433: "Shared Scene Setup 2433",
    2434: "Shared Scene Setup 2434", 2435: "Shared Scene Setup 2435",
    2436: "Shared Scene Setup 2436", 2437: "Shared Scene Setup 2437",
    2438: "Shared Scene Setup 2438", 2439: "Shared Scene Setup 2439",
    2440: "Shared Scene Setup 2440", 2441: "Shared Scene Setup 2441",
    2442: "Shared Scene Setup 2442", 2443: "Shared Scene Setup 2443",
    2444: "Shared Scene Setup 2444", 2445: "Shared Scene Setup 2445",
    2446: "Shared Scene Setup 2446", 2447: "Shared Scene Setup 2447",
    2448: "Shared Scene Setup 2448", 2449: "Shared Scene Setup 2449",
    2450: "Shared Scene Setup 2450", 2451: "Shared Scene Setup 2451",
    2452: "Shared Scene Setup 2452", 2453: "Shared Scene Setup 2453",
    2454: "Shared Scene Setup 2454", 2455: "Shared Scene Setup 2455",
    2456: "Shared Scene Setup 2456", 2457: "Shared Scene Setup 2457",
    2458: "Shared Scene Setup 2458", 2459: "Shared Scene Setup 2459",
    2460: "SG Shangri-La", 2461: "Shared Scene Setup 2461",
    2462: "Shared Scene Setup 2462", 2463: "SG Room",
    2464: "Shared Scene Setup 2464", 2465: "Shared Scene Setup 2465",
    2466: "Shared Scene Setup 2466", 2467: "Shared Scene Setup 2467",
    2468: "Shared Scene Setup 2468", 2469: "Shared Scene Setup 2469",
    2470: "Shared Scene Setup 2470", 2471: "Shared Scene Setup 2471",
    2472: "SG Shangri-La", 2473: "Shared Scene Setup 2473",
    2474: "Shared Scene Setup 2474", 2475: "Shared Scene Setup 2475",
    2476: "Shared Scene Setup 2476", 2477: "Shared Scene Setup 2477",
    2478: "Shared Scene Setup 2478", 2479: "Shared Scene Setup 2479",
    2480: "Shared Scene Setup 2480", 2481: "Shared Scene Setup 2481",
    2482: "Shared Scene Setup 2482", 2483: "Shared Scene Setup 2483",
    2484: "Shared Scene Setup 2484", 2485: "Shared Scene Setup 2485",
    2486: "Shared Scene Setup 2486", 2487: "Shared Scene Setup 2487",
    2488: "Shared Scene Setup 2488", 2489: "Shared Scene Setup 2489",
    2490: "Shared Scene Setup 2490", 2491: "Shared Scene Setup 2491",
    2492: "Shared Scene Setup 2492", 2493: "Shared Scene Setup 2493",
    2494: "Shared Scene Setup 2494", 2495: "Shared Scene Setup 2495",
    2496: "Shared Scene Setup 2496", 2497: "Shared Scene Setup 2497",
    2498: "Shared Scene Setup 2498", 2499: "Shared Scene Setup 2499",
    2500: "Shared Scene Setup 2500", 2501: "Shared Scene Setup 2501",
    2502: "Shared Scene Setup 2502", 2503: "Shared Scene Setup 2503",
    2504: "Shared Scene Setup 2504", 2505: "Shared Scene Setup 2505",
    2506: "Shared Scene Setup 2506", 2507: "SG Room",
    2508: "Shared Scene Setup 2508", 2509: "Shared Scene Setup 2509",
    2510: "Shared Scene Setup 2510", 2511: "Shared Scene Setup 2511",
    2512: "Shared Scene Setup 2512", 2513: "Shared Scene Setup 2513",
    2514: "Shared Scene Setup 2514", 2515: "Shared Scene Setup 2515",
    2516: "Shared Scene Setup 2516", 2517: "Shared Scene Setup 2517",
    2518: "Shared Scene Setup 2518", 2519: "Shared Scene Setup 2519",
    2520: "Shared Scene Setup 2520", 2521: "Shared Scene Setup 2521",
    2522: "Shared Scene Setup 2522", 2523: "Shared Scene Setup 2523",
    2524: "Shared Scene Setup 2524", 2525: "Shared Scene Setup 2525",
    2526: "Shared Scene Setup 2526", 2527: "Shared Scene Setup 2527",
    2528: "Shared Scene Setup 2528", 2529: "Shared Scene Setup 2529",
    2530: "Shared Scene Setup 2530", 2531: "Shared Scene Setup 2531",
    2532: "Shared Scene Setup 2532", 2533: "Shared Scene Setup 2533",
    2534: "Shared Scene Setup 2534", 2535: "Shared Scene Setup 2535",
    2536: "SG Room", 2537: "Shared Scene Setup 2537",
    2538: "Shared Scene Setup 2538", 2539: "Shared Scene Setup 2539",
    2540: "Shared Scene Setup 2540", 2541: "SG Room", 2542: "SG Tria Region",
    2543: "Shared Scene Setup 2543", 2544: "Shared Scene Setup 2544",
    2545: "Shared Scene Setup 2545", 2546: "Shared Scene Setup 2546",
    2547: "Shared Scene Setup 2547", 2548: "Shared Scene Setup 2548",
    2549: "Shared Scene Setup 2549", 2550: "Shared Scene Setup 2550",
    2551: "Shared Scene Setup 2551", 2552: "Shared Scene Setup 2552",
    2553: "Shared Scene Setup 2553", 2554: "Shared Scene Setup 2554",
    2555: "Shared Scene Setup 2555", 2556: "Shared Scene Setup 2556",
    2557: "Shared Scene Setup 2557", 2558: "Shared Scene Setup 2558",
    2559: "Shared Scene Setup 2559", 2560: "Shared Scene Setup 2560",
    2561: "Shared Scene Setup 2561", 2562: "Shared Scene Setup 2562",
    2563: "Shared Scene Setup 2563", 2564: "Shared Scene Setup 2564",
    2565: "Shared Scene Setup 2565", 2566: "Shared Scene Setup 2566",
    2567: "Shared Scene Setup 2567", 2568: "Shared Scene Setup 2568",
    2569: "Shared Scene Setup 2569", 2570: "Shared Scene Setup 2570",
    2571: "Shared Scene Setup 2571", 2572: "Shared Scene Setup 2572",
    2573: "Shared Scene Setup 2573", 2574: "Shared Scene Setup 2574",
    2575: "Shared Scene Setup 2575", 2576: "Shared Scene Setup 2576",
    2577: "Shared Scene Setup 2577", 2578: "Shared Scene Setup 2578",
    2579: "Shared Scene Setup 2579", 2580: "Shared Scene Setup 2580",
    2581: "Shared Scene Setup 2581", 2582: "Shared Scene Setup 2582",
    2583: "Shared Scene Setup 2583", 2584: "Shared Scene Setup 2584",
    2585: "Shared Scene Setup 2585", 2586: "Shared Scene Setup 2586",
    2587: "Shared Scene Setup 2587", 2588: "Shared Scene Setup 2588",
    2589: "Shared Scene Setup 2589", 2590: "SG Alkaico General Store",
    2591: "Shared Scene Setup 2591", 2592: "Shared Scene Setup 2592",
    2593: "Shared Scene Setup 2593", 2594: "Shared Scene Setup 2594",
    2595: "Shared Scene Setup 2595", 2596: "Shared Scene Setup 2596",
    2597: "Shared Scene Setup 2597", 2598: "Shared Scene Setup 2598",
    2599: "Shared Scene Setup 2599", 2600: "Shared Scene Setup 2600",
    2601: "Shared Scene Setup 2601", 2602: "Shared Scene Setup 2602",
    2603: "Shared Scene Setup 2603", 2604: "Shared Scene Setup 2604",
    2605: "Shared Scene Setup 2605", 2606: "Shared Scene Setup 2606",
    2607: "Shared Scene Setup 2607", 2608: "Shared Scene Setup 2608",
    2609: "Shared Scene Setup 2609", 2610: "Shared Scene Setup 2610",
    2611: "Shared Scene Setup 2611", 2612: "Shared Scene Setup 2612",
    2613: "Shared Scene Setup 2613", 2614: "Shared Scene Setup 2614",
    2615: "Shared Scene Setup 2615", 2616: "Shared Scene Setup 2616",
    2617: "Shared Scene Setup 2617", 2618: "Shared Scene Setup 2618",
    2619: "Shared Scene Setup 2619", 2620: "Shared Scene Setup 2620",
    2621: "Shared Scene Setup 2621", 2622: "Shared Scene Setup 2622",
    2623: "Shared Scene Setup 2623", 2624: "Shared Scene Setup 2624",
    2625: "Shared Scene Setup 2625", 2626: "Shared Scene Setup 2626",
    2627: "Shared Scene Setup 2627", 2628: "Shared Scene Setup 2628",
    2629: "Shared Scene Setup 2629", 2630: "Shared Scene Setup 2630",
    2631: "Shared Scene Setup 2631", 2632: "Shared Scene Setup 2632",
    2633: "Shared Scene Setup 2633", 2634: "Shared Scene Setup 2634",
    2635: "Shared Scene Setup 2635", 2636: "Shared Scene Setup 2636",
    2637: "Shared Scene Setup 2637", 2638: "Shared Scene Setup 2638",
    2639: "Shared Scene Setup 2639", 2640: "Shared Scene Setup 2640",
    2641: "Shared Scene Setup 2641", 2642: "Shared Scene Setup 2642",
    2643: "Shared Scene Setup 2643", 2644: "Shared Scene Setup 2644",
    2645: "Shared Scene Setup 2645", 2646: "Shared Scene Setup 2646",
    2647: "Shared Scene Setup 2647", 2648: "Shared Scene Setup 2648",
    2649: "Shared Scene Setup 2649", 2650: "Shared Scene Setup 2650",
    2651: "Shared Scene Setup 2651", 2652: "Shared Scene Setup 2652",
    2653: "Shared Scene Setup 2653", 2654: "Shared Scene Setup 2654",
    2655: "Shared Scene Setup 2655", 2656: "Shared Scene Setup 2656",
    2657: "Shared Scene Setup 2657", 2658: "Shared Scene Setup 2658",
    2659: "Shared Scene Setup 2659", 2660: "Shared Scene Setup 2660",
    2661: "Shared Scene Setup 2661", 2662: "Shared Scene Setup 2662",
    2663: "Shared Scene Setup 2663", 2664: "Shared Scene Setup 2664",
    2665: "Shared Scene Setup 2665", 2666: "Shared Scene Setup 2666",
    2667: "Shared Scene Setup 2667", 2668: "Shared Scene Setup 2668",
    2669: "Shared Scene Setup 2669", 2670: "Shared Scene Setup 2670",
    2671: "Shared Scene Setup 2671", 2672: "Shared Scene Setup 2672",
    2673: "Shared Scene Setup 2673", 2674: "Shared Scene Setup 2674",
    2675: "Shared Scene Setup 2675", 2676: "Shared Scene Setup 2676",
    2677: "Shared Scene Setup 2677", 2678: "Shared Scene Setup 2678",
    2679: "Shared Scene Setup 2679", 2680: "Shared Scene Setup 2680",
    2681: "Shared Scene Setup 2681", 2682: "Shared Scene Setup 2682",
    2683: "Shared Scene Setup 2683", 2684: "Shared Scene Setup 2684",
    2685: "Shared Scene Setup 2685", 2686: "Shared Scene Setup 2686",
    2687: "Shared Scene Setup 2687", 2688: "Shared Scene Setup 2688",
    2689: "Shared Scene Setup 2689", 2690: "Shared Scene Setup 2690",
    2691: "Shared Scene Setup 2691", 2692: "Shared Scene Setup 2692",
    2693: "Shared Scene Setup 2693", 2694: "Shared Scene Setup 2694",
    2695: "Shared Scene Setup 2695", 2696: "Shared Scene Setup 2696",
    2697: "Shared Scene Setup 2697", 2698: "Shared Scene Setup 2698",
    2699: "Shared Scene Setup 2699", 2700: "Shared Scene Setup 2700",
    2701: "Shared Scene Setup 2701", 2702: "Shared Scene Setup 2702",
    2703: "Shared Scene Setup 2703", 2704: "Shared Scene Setup 2704",
    2705: "Shared Scene Setup 2705", 2706: "Shared Scene Setup 2706",
    2707: "Shared Scene Setup 2707", 2708: "Shared Scene Setup 2708",
    2709: "Shared Scene Setup 2709", 2710: "Shared Scene Setup 2710",
    2711: "Shared Scene Setup 2711", 2712: "Shared Scene Setup 2712",
    2713: "Shared Scene Setup 2713", 2714: "Shared Scene Setup 2714",
    2715: "Shared Scene Setup 2715", 2716: "Shared Scene Setup 2716",
    2717: "Shared Scene Setup 2717", 2718: "Shared Scene Setup 2718",
    2719: "Shared Scene Setup 2719", 2720: "Shared Scene Setup 2720",
    2721: "Shared Scene Setup 2721", 2722: "Shared Scene Setup 2722",
    2723: "Shared Scene Setup 2723", 2724: "Shared Scene Setup 2724",
    2725: "Shared Scene Setup 2725", 2726: "Shared Scene Setup 2726",
    2727: "Shared Scene Setup 2727", 2728: "Shared Scene Setup 2728",
    2729: "Shared Scene Setup 2729", 2730: "Shared Scene Setup 2730",
    2731: "Shared Scene Setup 2731", 2732: "Shared Scene Setup 2732",
    2733: "Shared Scene Setup 2733", 2734: "Shared Scene Setup 2734",
    2735: "Shared Scene Setup 2735", 2736: "Shared Scene Setup 2736",
    2737: "Shared Scene Setup 2737", 2738: "Shared Scene Setup 2738",
    2739: "Shared Scene Setup 2739", 2740: "Shared Scene Setup 2740",
    2741: "Shared Scene Setup 2741", 2742: "Shared Scene Setup 2742",
    2743: "Shared Scene Setup 2743", 2744: "Shared Scene Setup 2744",
    2745: "Shared Scene Setup 2745", 2746: "Shared Scene Setup 2746",
    2747: "Shared Scene Setup 2747", 2748: "Shared Scene Setup 2748",
    2749: "Shared Scene Setup 2749", 2750: "Shared Scene Setup 2750",
    2751: "Shared Scene Setup 2751", 2752: "Shared Scene Setup 2752",
    2753: "Shared Scene Setup 2753", 2754: "Shared Scene Setup 2754",
    2755: "Shared Scene Setup 2755", 2756: "Shared Scene Setup 2756",
    2757: "Shared Scene Setup 2757", 2758: "Shared Scene Setup 2758",
    2759: "Shared Scene Setup 2759", 2760: "Shared Scene Setup 2760",
    2761: "Shared Scene Setup 2761", 2762: "Shared Scene Setup 2762",
    2763: "Shared Scene Setup 2763", 2764: "Shared Scene Setup 2764",
    2765: "Shared Scene Setup 2765", 2766: "Shared Scene Setup 2766",
    2767: "Shared Scene Setup 2767", 2768: "Shared Scene Setup 2768",
    2769: "Shared Scene Setup 2769", 2770: "Shared Scene Setup 2770",
    2771: "Shared Scene Setup 2771", 2772: "Shared Scene Setup 2772",
    2773: "Shared Scene Setup 2773", 2774: "Shared Scene Setup 2774",
    2775: "Shared Scene Setup 2775", 2776: "Shared Scene Setup 2776",
    2777: "Shared Scene Setup 2777", 2778: "Shared Scene Setup 2778",
    2779: "Shared Scene Setup 2779", 2780: "Shared Scene Setup 2780",
    2781: "Shared Scene Setup 2781", 2782: "Shared Scene Setup 2782",
    2783: "Shared Scene Setup 2783", 2784: "Shared Scene Setup 2784",
    2785: "Shared Scene Setup 2785", 2786: "Shared Scene Setup 2786",
    2787: "SG Dorse Region", 2788: "Shared Scene Setup 2788",
    2789: "Shared Scene Setup 2789", 2790: "Shared Scene Setup 2790",
    2791: "Shared Scene Setup 2791", 2792: "Shared Scene Setup 2792",
    2793: "Shared Scene Setup 2793", 2794: "Shared Scene Setup 2794",
    2795: "Shared Scene Setup 2795", 2796: "Shared Scene Setup 2796",
    2797: "Shared Scene Setup 2797", 2798: "SG Dorse Region",
    2799: "Shared Scene Setup 2799", 2800: "Shared Scene Setup 2800",
    2801: "Shared Scene Setup 2801", 2802: "SG Dorse Region",
    2803: "Shared Scene Setup 2803", 2804: "Shared Scene Setup 2804",
    2805: "Shared Scene Setup 2805", 2806: "Shared Scene Setup 2806",
    2807: "Shared Scene Setup 2807", 2808: "Shared Scene Setup 2808",
    2809: "Shared Scene Setup 2809", 2810: "Shared Scene Setup 2810",
    2811: "Shared Scene Setup 2811", 2812: "Shared Scene Setup 2812",
    2813: "Shared Scene Setup 2813", 2814: "Shared Scene Setup 2814",
    2815: "Shared Scene Setup 2815", 2816: "Shared Scene Setup 2816",
    2817: "Shared Scene Setup 2817", 2818: "Shared Scene Setup 2818",
    2819: "Shared Scene Setup 2819", 2820: "Shared Scene Setup 2820",
    2821: "Shared Scene Setup 2821", 2822: "Shared Scene Setup 2822",
    2823: "Shared Scene Setup 2823", 2824: "Shared Scene Setup 2824",
    2825: "Shared Scene Setup 2825", 2826: "Shared Scene Setup 2826",
    2827: "Shared Scene Setup 2827", 2828: "Shared Scene Setup 2828",
    2829: "Shared Scene Setup 2829", 2830: "Shared Scene Setup 2830",
    2831: "Shared Scene Setup 2831", 2832: "Shared Scene Setup 2832",
    2833: "Shared Scene Setup 2833", 2834: "Shared Scene Setup 2834",
    2835: "Shared Scene Setup 2835", 2836: "Shared Scene Setup 2836",
    2837: "Shared Scene Setup 2837", 2838: "Shared Scene Setup 2838",
    2839: "Shared Scene Setup 2839", 2840: "Shared Scene Setup 2840",
    2841: "Shared Scene Setup 2841", 2842: "Shared Scene Setup 2842",
    2843: "Shared Scene Setup 2843", 2844: "Shared Scene Setup 2844",
    2845: "Shared Scene Setup 2845", 2846: "Shared Scene Setup 2846",
    2847: "Shared Scene Setup 2847", 2848: "Shared Scene Setup 2848",
    2849: "Shared Scene Setup 2849", 2850: "Shared Scene Setup 2850",
    2851: "Shared Scene Setup 2851", 2852: "Shared Scene Setup 2852",
    2853: "Shared Scene Setup 2853", 2854: "Shared Scene Setup 2854",
    2855: "Shared Scene Setup 2855", 2856: "Shared Scene Setup 2856",
    2857: "Shared Scene Setup 2857", 2858: "Shared Scene Setup 2858",
    2859: "Shared Scene Setup 2859", 2860: "Shared Scene Setup 2860",
    2861: "Shared Scene Setup 2861", 2862: "Shared Scene Setup 2862",
    2863: "Shared Scene Setup 2863", 2864: "Shared Scene Setup 2864",
    2865: "Shared Scene Setup 2865", 2866: "Shared Scene Setup 2866",
    2867: "Shared Scene Setup 2867", 2868: "Shared Scene Setup 2868",
    2869: "Shared Scene Setup 2869", 2870: "Shared Scene Setup 2870",
    2871: "Shared Scene Setup 2871", 2872: "Shared Scene Setup 2872",
    2873: "Shared Scene Setup 2873", 2874: "Shared Scene Setup 2874",
    2875: "Shared Scene Setup 2875", 2876: "Shared Scene Setup 2876",
    2877: "Shared Scene Setup 2877", 2878: "Shared Scene Setup 2878",
    2879: "Shared Scene Setup 2879", 2880: "Shared Scene Setup 2880",
    2881: "Shared Scene Setup 2881", 2882: "Shared Scene Setup 2882",
    2883: "Shared Scene Setup 2883", 2884: "Shared Scene Setup 2884",
    2885: "Shared Scene Setup 2885", 2886: "Shared Scene Setup 2886",
    2887: "Shared Scene Setup 2887", 2888: "Shared Scene Setup 2888",
    2889: "Shared Scene Setup 2889", 2890: "Shared Scene Setup 2890",
    2891: "Shared Scene Setup 2891", 2892: "Shared Scene Setup 2892",
    2893: "Shared Scene Setup 2893", 2894: "Shared Scene Setup 2894",
    2895: "Shared Scene Setup 2895", 2896: "Shared Scene Setup 2896",
    2897: "Shared Scene Setup 2897", 2898: "Shared Scene Setup 2898",
    2899: "Shared Scene Setup 2899", 2900: "Shared Scene Setup 2900",
    2901: "Shared Scene Setup 2901", 2902: "Shared Scene Setup 2902",
    2903: "Shared Scene Setup 2903", 2904: "Shared Scene Setup 2904",
    2905: "Shared Scene Setup 2905", 2906: "Shared Scene Setup 2906",
    2907: "Shared Scene Setup 2907", 2908: "Shared Scene Setup 2908",
    2909: "Shared Scene Setup 2909", 2910: "Shared Scene Setup 2910",
    2911: "Shared Scene Setup 2911", 2912: "Shared Scene Setup 2912",
    2913: "Shared Scene Setup 2913", 2914: "Shared Scene Setup 2914",
    2915: "Shared Scene Setup 2915", 2916: "Shared Scene Setup 2916",
    2917: "Shared Scene Setup 2917", 2918: "Shared Scene Setup 2918",
    2919: "Shared Scene Setup 2919", 2920: "Shared Scene Setup 2920",
    2921: "Shared Scene Setup 2921", 2922: "Shared Scene Setup 2922",
    2923: "Shared Scene Setup 2923", 2924: "Shared Scene Setup 2924",
    2925: "Shared Scene Setup 2925", 2926: "Shared Scene Setup 2926",
    2927: "Shared Scene Setup 2927", 2928: "Shared Scene Setup 2928",
    2929: "Shared Scene Setup 2929", 2930: "Shared Scene Setup 2930",
    2931: "Shared Scene Setup 2931", 2932: "Shared Scene Setup 2932",
    2933: "Shared Scene Setup 2933", 2934: "Shared Scene Setup 2934",
    2935: "Shared Scene Setup 2935", 2936: "Shared Scene Setup 2936",
    2937: "Shared Scene Setup 2937", 2938: "Shared Scene Setup 2938",
    2939: "Shared Scene Setup 2939", 2940: "Shared Scene Setup 2940",
    2941: "Shared Scene Setup 2941", 2942: "Shared Scene Setup 2942",
    2943: "Shared Scene Setup 2943", 2944: "Shared Scene Setup 2944",
    2945: "Shared Scene Setup 2945", 2946: "Shared Scene Setup 2946",
    2947: "Shared Scene Setup 2947", 2948: "Shared Scene Setup 2948",
    2949: "Shared Scene Setup 2949", 2950: "Shared Scene Setup 2950",
    2951: "Shared Scene Setup 2951", 2952: "Shared Scene Setup 2952",
    2953: "Shared Scene Setup 2953", 2954: "Shared Scene Setup 2954",
    2955: "Shared Scene Setup 2955", 2956: "Shared Scene Setup 2956",
    2957: "Shared Scene Setup 2957", 2958: "Shared Scene Setup 2958",
    2959: "Shared Scene Setup 2959", 2960: "Shared Scene Setup 2960",
    2961: "Shared Scene Setup 2961", 2962: "Shared Scene Setup 2962",
    2963: "Shared Scene Setup 2963", 2964: "Shared Scene Setup 2964",
    2965: "Shared Scene Setup 2965", 2966: "Shared Scene Setup 2966",
    2967: "Shared Scene Setup 2967", 2968: "Shared Scene Setup 2968",
    2969: "Shared Scene Setup 2969", 2970: "Shared Scene Setup 2970",
    2971: "SG Tigers Apartments 2nd Floor", 2972: "Shared Scene Setup 2972",
    2973: "Shared Scene Setup 2973", 2974: "Shared Scene Setup 2974",
    2975: "Shared Scene Setup 2975", 2976: "Shared Scene Setup 2976",
    2977: "Shared Scene Setup 2977", 2978: "Shared Scene Setup 2978",
    2979: "Shared Scene Setup 2979", 2980: "Shared Scene Setup 2980",
    2981: "Shared Scene Setup 2981", 2982: "Shared Scene Setup 2982",
    2983: "Shared Scene Setup 2983", 2984: "Shared Scene Setup 2984",
    2985: "Shared Scene Setup 2985", 2986: "Shared Scene Setup 2986",
    2987: "Shared Scene Setup 2987", 2988: "Shared Scene Setup 2988",
    2989: "Shared Scene Setup 2989", 2990: "Shared Scene Setup 2990",
    2991: "Shared Scene Setup 2991", 2992: "Shared Scene Setup 2992",
    2993: "Shared Scene Setup 2993", 2994: "Shared Scene Setup 2994",
    2995: "Shared Scene Setup 2995", 2996: "Shared Scene Setup 2996",
    2997: "Shared Scene Setup 2997", 2998: "Shared Scene Setup 2998",
    2999: "Shared Scene Setup 2999", 3000: "Shared Scene Setup 3000",
    3001: "Shared Scene Setup 3001", 3002: "Shared Scene Setup 3002",
    3003: "Shared Scene Setup 3003", 3004: "Shared Scene Setup 3004",
    3005: "Shared Scene Setup 3005", 3006: "Shared Scene Setup 3006",
    3007: "Shared Scene Setup 3007", 3008: "Shared Scene Setup 3008",
    3009: "Shared Scene Setup 3009", 3010: "Shared Scene Setup 3010",
    3011: "Shared Scene Setup 3011", 3012: "Shared Scene Setup 3012",
    3013: "Shared Scene Setup 3013", 3014: "Shared Scene Setup 3014",
    3015: "Shared Scene Setup 3015", 3016: "Shared Scene Setup 3016",
    3017: "Shared Scene Setup 3017", 3018: "Shared Scene Setup 3018",
    3019: "Shared Scene Setup 3019", 3020: "Shared Scene Setup 3020",
    3021: "Shared Scene Setup 3021", 3022: "Shared Scene Setup 3022",
    3023: "Shared Scene Setup 3023", 3024: "Shared Scene Setup 3024",
    3025: "Shared Scene Setup 3025", 3026: "Shared Scene Setup 3026",
    3027: "Shared Scene Setup 3027", 3028: "Shared Scene Setup 3028",
    3029: "Shared Scene Setup 3029", 3030: "SG Tigers Apartments 2nd Floor",
    3031: "Shared Scene Setup 3031", 3032: "Shared Scene Setup 3032",
    3033: "Shared Scene Setup 3033", 3034: "Shared Scene Setup 3034",
    3035: "Shared Scene Setup 3035", 3036: "SG Alkaico General Store",
    3037: "Shared Scene Setup 3037", 3038: "Shared Scene Setup 3038",
    3039: "Shared Scene Setup 3039", 3040: "Shared Scene Setup 3040",
    3041: "Shared Scene Setup 3041", 3042: "Shared Scene Setup 3042",
    3043: "Shared Scene Setup 3043", 3044: "Shared Scene Setup 3044",
    3045: "Shared Scene Setup 3045", 3046: "Shared Scene Setup 3046",
    3047: "Shared Scene Setup 3047", 3048: "Shared Scene Setup 3048",
    3049: "Shared Scene Setup 3049", 3050: "Shared Scene Setup 3050",
    3051: "Shared Scene Setup 3051", 3052: "Shared Scene Setup 3052",
    3053: "Shared Scene Setup 3053", 3054: "Shared Scene Setup 3054",
    3055: "Shared Scene Setup 3055", 3056: "Shared Scene Setup 3056",
    3057: "Shared Scene Setup 3057", 3058: "Shared Scene Setup 3058",
    3059: "Shared Scene Setup 3059", 3060: "Shared Scene Setup 3060",
    3061: "Shared Scene Setup 3061", 3062: "Shared Scene Setup 3062",
    3063: "Shared Scene Setup 3063", 3064: "Shared Scene Setup 3064",
    3065: "Shared Scene Setup 3065", 3066: "Shared Scene Setup 3066",
    3067: "Shared Scene Setup 3067", 3068: "Shared Scene Setup 3068",
    3069: "Shared Scene Setup 3069", 3070: "Shared Scene Setup 3070",
    3071: "Shared Scene Setup 3071", 3072: "Shared Scene Setup 3072",
    3073: "Shared Scene Setup 3073", 3074: "Shared Scene Setup 3074",
    3075: "Shared Scene Setup 3075", 3076: "Shared Scene Setup 3076",
    3077: "Shared Scene Setup 3077", 3078: "Shared Scene Setup 3078",
    3079: "Shared Scene Setup 3079", 3080: "Shared Scene Setup 3080",
    3081: "Shared Scene Setup 3081", 3082: "Shared Scene Setup 3082",
    3083: "Shared Scene Setup 3083", 3084: "Shared Scene Setup 3084",
    3085: "Shared Scene Setup 3085", 3086: "Shared Scene Setup 3086",
    3087: "Shared Scene Setup 3087", 3088: "Shared Scene Setup 3088",
    3089: "Shared Scene Setup 3089", 3090: "Shared Scene Setup 3090",
    3091: "Shared Scene Setup 3091", 3092: "Shared Scene Setup 3092",
    3093: "Shared Scene Setup 3093", 3094: "Shared Scene Setup 3094",
    3095: "Shared Scene Setup 3095", 3096: "Shared Scene Setup 3096",
    3097: "Shared Scene Setup 3097", 3098: "Shared Scene Setup 3098",
    3099: "Shared Scene Setup 3099", 3100: "Shared Scene Setup 3100",
    3101: "Shared Scene Setup 3101", 3102: "Shared Scene Setup 3102",
    3103: "Shared Scene Setup 3103", 3104: "Shared Scene Setup 3104",
    3105: "Shared Scene Setup 3105", 3106: "Shared Scene Setup 3106",
    3107: "Shared Scene Setup 3107", 3108: "Shared Scene Setup 3108",
    3109: "Shared Scene Setup 3109", 3110: "Shared Scene Setup 3110",
    3111: "Shared Scene Setup 3111", 3112: "Shared Scene Setup 3112",
    3113: "Shared Scene Setup 3113", 3114: "Shared Scene Setup 3114",
    3115: "Shared Scene Setup 3115", 3116: "Shared Scene Setup 3116",
    3117: "Shared Scene Setup 3117", 3118: "Shared Scene Setup 3118",
    3119: "Shared Scene Setup 3119", 3120: "Shared Scene Setup 3120",
    3121: "Shared Scene Setup 3121", 3122: "Shared Scene Setup 3122",
    3123: "Shared Scene Setup 3123", 3124: "Shared Scene Setup 3124",
    3125: "Shared Scene Setup 3125", 3126: "Shared Scene Setup 3126",
    3127: "Shared Scene Setup 3127", 3128: "Shared Scene Setup 3128",
    3129: "Shared Scene Setup 3129", 3130: "Shared Scene Setup 3130",
    3131: "Shared Scene Setup 3131", 3132: "Shared Scene Setup 3132",
    3133: "Shared Scene Setup 3133", 3134: "Shared Scene Setup 3134",
    3135: "Shared Scene Setup 3135", 3136: "Shared Scene Setup 3136",
    3137: "Shared Scene Setup 3137", 3138: "Shared Scene Setup 3138",
    3139: "Shared Scene Setup 3139", 3140: "Shared Scene Setup 3140",
    3141: "Shared Scene Setup 3141", 3142: "Shared Scene Setup 3142",
    3143: "Shared Scene Setup 3143", 3144: "Shared Scene Setup 3144",
    3145: "Shared Scene Setup 3145", 3146: "Shared Scene Setup 3146",
    3147: "Shared Scene Setup 3147", 3148: "Shared Scene Setup 3148",
    3149: "Shared Scene Setup 3149", 3150: "Shared Scene Setup 3150",
    3151: "Shared Scene Setup 3151", 3152: "Shared Scene Setup 3152",
    3153: "Shared Scene Setup 3153", 3154: "Shared Scene Setup 3154",
    3155: "Shared Scene Setup 3155", 3156: "Shared Scene Setup 3156",
    3157: "Shared Scene Setup 3157", 3158: "Shared Scene Setup 3158",
    3159: "Shared Scene Setup 3159", 3160: "Shared Scene Setup 3160",
    3161: "Shared Scene Setup 3161", 3162: "Shared Scene Setup 3162",
    3163: "Shared Scene Setup 3163", 3164: "Shared Scene Setup 3164",
    3165: "Shared Scene Setup 3165", 3166: "Shared Scene Setup 3166",
    3167: "Shared Scene Setup 3167", 3168: "Shared Scene Setup 3168",
    3169: "Shared Scene Setup 3169", 3170: "Shared Scene Setup 3170",
    3171: "Shared Scene Setup 3171", 3172: "Shared Scene Setup 3172",
    3173: "Shared Scene Setup 3173", 3174: "Shared Scene Setup 3174",
    3175: "Shared Scene Setup 3175", 3176: "Shared Scene Setup 3176",
    3177: "Shared Scene Setup 3177", 3178: "Shared Scene Setup 3178",
    3179: "Shared Scene Setup 3179", 3180: "Shared Scene Setup 3180",
    3181: "Shared Scene Setup 3181", 3182: "Shared Scene Setup 3182",
    3183: "Shared Scene Setup 3183", 3184: "Shared Scene Setup 3184",
    3185: "Shared Scene Setup 3185", 3186: "Shared Scene Setup 3186",
    3187: "Shared Scene Setup 3187", 3188: "Shared Scene Setup 3188",
    3189: "Shared Scene Setup 3189", 3190: "Shared Scene Setup 3190",
    3191: "Shared Scene Setup 3191", 3192: "Shared Scene Setup 3192",
    3193: "Shared Scene Setup 3193", 3194: "Shared Scene Setup 3194",
    3195: "Shared Scene Setup 3195", 3196: "Shared Scene Setup 3196",
    3197: "Shared Scene Setup 3197", 3198: "Shared Scene Setup 3198",
    3199: "Shared Scene Setup 3199", 3200: "Shared Scene Setup 3200",
    3201: "Shared Scene Setup 3201", 3202: "Shared Scene Setup 3202",
    3203: "Shared Scene Setup 3203", 3204: "Shared Scene Setup 3204",
    3205: "Shared Scene Setup 3205", 3206: "Shared Scene Setup 3206",
    3207: "Shared Scene Setup 3207", 3208: "Shared Scene Setup 3208",
    3209: "Shared Scene Setup 3209", 3210: "Shared Scene Setup 3210",
    3211: "Shared Scene Setup 3211", 3212: "Shared Scene Setup 3212",
    3213: "Shared Scene Setup 3213", 3214: "Shared Scene Setup 3214",
    3215: "Shared Scene Setup 3215", 3216: "Shared Scene Setup 3216",
    3217: "Shared Scene Setup 3217", 3218: "Shared Scene Setup 3218",
    3219: "Shared Scene Setup 3219", 3220: "Shared Scene Setup 3220",
    3221: "Shared Scene Setup 3221", 3222: "Shared Scene Setup 3222",
    3223: "SG Alkaico General Store", 3224: "Shared Scene Setup 3224",
    3225: "Shared Scene Setup 3225", 3226: "Shared Scene Setup 3226",
    3227: "Shared Scene Setup 3227", 3228: "Shared Scene Setup 3228",
    3229: "Shared Scene Setup 3229", 3230: "Shared Scene Setup 3230",
    3231: "Shared Scene Setup 3231", 3232: "Shared Scene Setup 3232",
    3233: "Shared Scene Setup 3233", 3234: "Shared Scene Setup 3234",
    3235: "Shared Scene Setup 3235", 3236: "Shared Scene Setup 3236",
    3237: "Shared Scene Setup 3237", 3238: "Shared Scene Setup 3238",
    3239: "Shared Scene Setup 3239", 3240: "Shared Scene Setup 3240",
    3241: "Shared Scene Setup 3241", 3242: "Shared Scene Setup 3242",
    3243: "Shared Scene Setup 3243", 3244: "Shared Scene Setup 3244",
    3245: "Shared Scene Setup 3245", 3246: "Shared Scene Setup 3246",
    3247: "Shared Scene Setup 3247", 3248: "Shared Scene Setup 3248",
    3249: "Shared Scene Setup 3249", 3250: "SG Tigers Apartments 2nd Floor",
    3251: "Shared Scene Setup 3251", 3252: "Shared Scene Setup 3252",
    3253: "Shared Scene Setup 3253", 3254: "Shared Scene Setup 3254",
    3255: "Shared Scene Setup 3255", 3256: "Shared Scene Setup 3256",
    3257: "Shared Scene Setup 3257", 3258: "Shared Scene Setup 3258",
    3259: "Shared Scene Setup 3259", 3260: "Shared Scene Setup 3260",
    3261: "Shared Scene Setup 3261", 3262: "Shared Scene Setup 3262",
    3263: "Shared Scene Setup 3263", 3264: "Shared Scene Setup 3264",
    3265: "Shared Scene Setup 3265", 3266: "Shared Scene Setup 3266",
    3267: "Shared Scene Setup 3267", 3268: "Shared Scene Setup 3268",
    3269: "Shared Scene Setup 3269", 3270: "Shared Scene Setup 3270",
    3271: "Shared Scene Setup 3271", 3272: "Shared Scene Setup 3272",
    3273: "Shared Scene Setup 3273", 3274: "Shared Scene Setup 3274",
    3275: "Shared Scene Setup 3275", 3276: "Shared Scene Setup 3276",
    3277: "Shared Scene Setup 3277", 3278: "Shared Scene Setup 3278",
    3279: "Shared Scene Setup 3279", 3280: "Shared Scene Setup 3280",
    3281: "Shared Scene Setup 3281", 3282: "Shared Scene Setup 3282",
    3283: "Shared Scene Setup 3283", 3284: "Shared Scene Setup 3284",
    3285: "Shared Scene Setup 3285", 3286: "Shared Scene Setup 3286",
    3287: "Shared Scene Setup 3287", 3288: "Shared Scene Setup 3288",
    3289: "Shared Scene Setup 3289", 3290: "Shared Scene Setup 3290",
    3291: "Shared Scene Setup 3291", 3292: "Shared Scene Setup 3292",
    3293: "Shared Scene Setup 3293", 3294: "Shared Scene Setup 3294",
    3295: "Shared Scene Setup 3295", 3296: "Shared Scene Setup 3296",
    3297: "Shared Scene Setup 3297", 3298: "Shared Scene Setup 3298",
    3299: "Shared Scene Setup 3299", 3300: "Shared Scene Setup 3300",
    3301: "Shared Scene Setup 3301", 3302: "Shared Scene Setup 3302",
    3303: "Shared Scene Setup 3303", 3304: "Shared Scene Setup 3304",
    3305: "Shared Scene Setup 3305", 3306: "Shared Scene Setup 3306",
    3307: "Shared Scene Setup 3307", 3308: "Shared Scene Setup 3308",
    3309: "Shared Scene Setup 3309", 3310: "Shared Scene Setup 3310",
    3311: "Shared Scene Setup 3311", 3312: "Shared Scene Setup 3312",
    3313: "Shared Scene Setup 3313", 3314: "Shared Scene Setup 3314",
    3315: "Shared Scene Setup 3315", 3316: "Shared Scene Setup 3316",
    3317: "Shared Scene Setup 3317", 3318: "Shared Scene Setup 3318",
    3319: "Shared Scene Setup 3319", 3320: "Shared Scene Setup 3320",
    3321: "Shared Scene Setup 3321", 3322: "Shared Scene Setup 3322",
    3323: "Shared Scene Setup 3323", 3324: "Shared Scene Setup 3324",
    3325: "Shared Scene Setup 3325", 3326: "Shared Scene Setup 3326",
    3327: "Shared Scene Setup 3327", 3328: "Shared Scene Setup 3328",
    3329: "Shared Scene Setup 3329", 3330: "Shared Scene Setup 3330",
    3331: "Shared Scene Setup 3331", 3332: "Shared Scene Setup 3332",
    3333: "Shared Scene Setup 3333", 3334: "Shared Scene Setup 3334",
    3335: "Shared Scene Setup 3335", 3336: "Shared Scene Setup 3336",
    3337: "Shared Scene Setup 3337", 3338: "Shared Scene Setup 3338",
    3339: "Shared Scene Setup 3339", 3340: "Shared Scene Setup 3340",
    3341: "Shared Scene Setup 3341", 3342: "Shared Scene Setup 3342",
    3343: "Shared Scene Setup 3343", 3344: "Shared Scene Setup 3344",
    3345: "Shared Scene Setup 3345", 3346: "Shared Scene Setup 3346",
    3347: "Shared Scene Setup 3347", 3348: "Shared Scene Setup 3348",
    3349: "Shared Scene Setup 3349", 3350: "Shared Scene Setup 3350",
    3351: "Shared Scene Setup 3351", 3352: "Shared Scene Setup 3352",
    3353: "Shared Scene Setup 3353", 3354: "Shared Scene Setup 3354",
    3355: "Shared Scene Setup 3355", 3356: "Shared Scene Setup 3356",
    3357: "Shared Scene Setup 3357", 3358: "Shared Scene Setup 3358",
    3359: "Shared Scene Setup 3359", 3360: "Shared Scene Setup 3360",
    3361: "Shared Scene Setup 3361", 3362: "Shared Scene Setup 3362",
    3363: "Shared Scene Setup 3363", 3364: "Shared Scene Setup 3364",
    3365: "Shared Scene Setup 3365", 3366: "Shared Scene Setup 3366",
    3367: "Shared Scene Setup 3367", 3368: "Shared Scene Setup 3368",
    3369: "Shared Scene Setup 3369", 3370: "Shared Scene Setup 3370",
    3371: "Shared Scene Setup 3371", 3372: "Shared Scene Setup 3372",
    3373: "Shared Scene Setup 3373", 3374: "Shared Scene Setup 3374",
    3375: "Shared Scene Setup 3375", 3376: "Shared Scene Setup 3376",
    3377: "Shared Scene Setup 3377", 3378: "Shared Scene Setup 3378",
    3379: "Shared Scene Setup 3379", 3380: "Shared Scene Setup 3380",
    3381: "Shared Scene Setup 3381", 3382: "Shared Scene Setup 3382",
    3383: "Shared Scene Setup 3383", 3384: "Shared Scene Setup 3384",
    3385: "Shared Scene Setup 3385", 3386: "Shared Scene Setup 3386",
    3387: "Shared Scene Setup 3387", 3388: "Shared Scene Setup 3388",
    3389: "Shared Scene Setup 3389", 3390: "Shared Scene Setup 3390",
    3391: "Shared Scene Setup 3391", 3392: "Shared Scene Setup 3392",
    3393: "Shared Scene Setup 3393", 3394: "Shared Scene Setup 3394",
    3395: "Shared Scene Setup 3395", 3396: "Shared Scene Setup 3396",
    3397: "Shared Scene Setup 3397", 3398: "Shared Scene Setup 3398",
    3399: "Shared Scene Setup 3399", 3400: "Shared Scene Setup 3400",
    3401: "Shared Scene Setup 3401", 3402: "Shared Scene Setup 3402",
    3403: "Shared Scene Setup 3403", 3404: "Shared Scene Setup 3404",
    3405: "Shared Scene Setup 3405", 3406: "Shared Scene Setup 3406",
    3407: "Shared Scene Setup 3407", 3408: "Shared Scene Setup 3408",
    3409: "Shared Scene Setup 3409", 3410: "Shared Scene Setup 3410",
    3411: "Shared Scene Setup 3411", 3412: "Animation 3412", 3413: "Animation 3413",
    3414: "Animation 3414", 3415: "Animation 3415", 3416: "Animation 3416",
    3417: "Animation 3417", 3418: "Animation 3418", 3419: "Animation 3419",
    3420: "Animation 3420", 3421: "Animation 3421", 3422: "Animation 3422",
    3423: "Animation 3423", 3424: "Animation 3424", 3425: "Animation 3425",
}

# Character animation packs (3426–3729)
ANIMATION_LABELS = {
    3426: "Jack", 3427: "Ganz", 3428: "Ridley", 3429: "Rynka", 3430: "Flau", 3431: "Star",
    3432: "Sebastian", 3433: "Genius", 3434: "Rocky", 3435: "Gawain", 3436: "Heavy Guardsman",
    3437: "Elwen", 3438: "Gerald", 3439: "Caesar", 3440: "Alicia", 3441: "Dennis", 3442: "Gareth",
    3443: "Gregory", 3444: "Walter", 3445: "Jarvis", 3446: "Light Guardsman", 3447: "Aldo",
    3448: "Gordon", 3449: "Bruce", 3450: "David", 3451: "Conrad", 3452: "Rolec", 3453: "Daniel",
    3454: "Carlos", 3455: "Gene", 3456: "Light Guardsman", 3457: "Thanos", 3458: "Curtis",
    3459: "Cecil", 3460: "Morgan", 3461: "Felix", 3462: "Jill", 3463: "Ursula", 3464: "Derek",
    3465: "Christoph", 3466: "Claudia", 3467: "Ardoph", 3468: "Dimitri", 3469: "Aidan",
    3470: "Cornelia", 3471: "Faraus", 3472: "Marietta", 3473: "Ernest", 3474: "Franklin",
    3475: "Johan", 3476: "Roche", 3477: "Light Guardsman", 3478: "Kain", 3479: "Fernando",
    3480: "Anastasia", 3481: "Dwight", 3482: "Godwin", 3483: "Achilles", 3484: "Flora",
    3485: "Elena", 3486: "Alvin", 3487: "Vitas", 3488: "Cosmo", 3489: "Grant", 3490: "Adina",
    3491: "Miranda", 3492: "Edgar", 3493: "Clive", 3494: "Lulu", 3495: "Eugene", 3496: "Nyx",
    3497: "Ortoroz", 3498: "Sonata", 3499: "Iris", 3500: "Nocturne", 3501: "Herz", 3502: "Alba",
    3503: "Lily", 3504: "Jared", 3505: "Pinky", 3506: "Interlude", 3507: "Solo", 3508: "Joaquel",
    3509: "Eon", 3510: "Elmo", 3511: "Jiorus", 3512: "Sarasenia", 3513: "Belflower",
    3514: "Jasne", 3515: "Larks", 3516: "Sakurazaki", 3517: "Junzaburo", 3518: "Natalie",
    3519: "Nina", 3520: "Charlie", 3521: "Leonard", 3522: "Light Guardsman", 3523: "Heavy Guardsman",
    3524: "Raymond", 3525: "Al", 3526: "Margaret", 3527: "Zion", 3528: "Paul", 3529: "Toma",
    3530: "Torenia", 3531: "Testa", 3532: "Nuse", 3533: "Jorn", 3534: "Barbena", 3535: "Giske",
    3536: "Yuri", 3537: "Warc", 3538: "Robin", 3539: "Sheila", 3540: "Jasmine", 3541: "Camuse",
    3542: "Lantana", 3543: "Lyle", 3544: "Rose", 3545: "Josef", 3546: "Virginia", 3547: "Morfinn",
    3548: "Bligh", 3549: "Freija", 3550: "Nask", 3551: "Cherie", 3552: "Zeke", 3553: "Dan",
    3554: "Servia", 3555: "Lunbar", 3556: "Sonia", 3557: "Startis", 3558: "Brood", 3559: "Garbella",
    3560: "Silvia", 3561: "Thyme", 3562: "Elef", 3563: "Ryan", 3564: "Hip", 3565: "Nick", 3566: "Kira",
    3567: "Rabi", 3568: "Golye", 3569: "Butch", 3570: "Sarval", 3571: "Sunset", 3572: "Sora",
    3573: "Keaton", 3574: "Tarkin", 3575: "Gonber", 3576: "Leban", 3577: "Mook", 3578: "Wal",
    3579: "Wyze", 3580: "Zeranium", 3581: "3581", 3582: "Pommelie", 3583: "Saron", 3584: "Cepheid",
    3585: "Baade", 3586: "Quasar", 3587: "Aphelion", 3588: "Gonovitch", 3589: "Albert", 3590: "Vladimir",
    3591: "Yevgeni", 3592: "Oleg", 3593: "Grigory", 3594: "Brockle", 3595: "Dyvad", 3596: "Gehrmann",
    3597: "Sergei", 3598: "Naom", 3599: "Aegenhart", 3600: "Marke", 3601: "Donovitch", 3602: "Zane",
    3603: "Hap", 3604: "Gil", 3605: "Shin", 3606: "Fan", 3607: "Row", 3608: "Pitt", 3609: "Few",
    3610: "Alan", 3611: "Keane", 3612: "Nogueira", 3613: "Clarence", 3614: "Serva", 3615: "Hyann",
    3616: "Chatt", 3617: "Zida", 3618: "Franz", 3619: "Romaria", 3620: "Marsha", 3621: "Lufa",
    3622: "Coco", 3623: "Martinez", 3624: "Santos", 3625: "Rika", 3626: "Mikey", 3627: "Gob",
    3628: "Lin", 3629: "Brie", 3630: "Gonn", 3631: "Golly", 3632: "Gobrey", 3633: "Den", 3634: "Ben",
    3635: "Aesop", 3636: "Monki", 3637: "Gabe", 3638: "Mason", 3639: "Goo", 3640: "Donkey",
    3641: "Ricky", 3642: "Drew", 3643: "Gruel", 3644: "Doppio", 3645: "Pietro", 3646: "Jan",
    3647: "Marco", 3648: "Niko", 3649: "Danny", 3650: "Dominic", 3651: "Bosso", 3652: "Georgio",
    3653: "Luka", 3654: "Sonny", 3655: "Giovanni", 3656: "Polpo", 3657: "JJ", 3658: "Leona",
    3659: "Leann", 3660: "Ray C Ross", 3661: "Pinta", 3662: "Buta", 3663: "Valkyrie", 3664: "Lezard",
    3665: "Radian", 3666: "Ethereal Queen", 3667: "Cairn", 3668: "Kelvin", 3669: "Gabriel Celesta",
    3670: "3670", 3671: "3671", 3672: "Galvados", 3673: "3673", 3674: "3674", 3675: "3675",
    3676: "3676", 3677: "3677", 3678: "Drago", 3679: "Bull", 3680: "3680", 3681: "3681",
    3682: "3682", 3683: "3683", 3684: "Library", 3685: "Phonograph", 3686: "Jack Bookshelf",
    3687: "Cross", 3688: "Stein", 3689: "Blackjack", 3690: "Event Watcher", 3691: "Parsec",
    3692: "Light Guardsman", 3693: "Light Guardsman", 3694: "Light Guardsman", 3695: "Heavy Guardsman",
    3696: "Heavy Guardsman", 3697: "Heavy Guardsman", 3698: "Heavy Guardsman", 3699: "Heavy Guardsman",
    3700: "Heavy Guardsman", 3701: "Heavy Guardsman", 3702: "Heavy Guardsman", 3703: "Heavy Guardsman",
    3704: "Cody", 3705: "Adele", 3706: "Howard", 3707: "Ravil", 3708: "Astor", 3709: "Maddock",
    3710: "Synelia", 3711: "Tony", 3712: "Patrick", 3713: "Putt", 3714: "Reynos", 3715: "Gobblehope IX",
    3716: "Nalshay", 3717: "Sayna", 3718: "Bran", 3719: "Stefan", 3720: "Mint", 3721: "Daria",
    3722: "Yack", 3723: "Lauren", 3724: "Theresa", 3725: "Garcia", 3726: "Dynas", 3727: "Epoch",
    3728: "Roy", 3729: "Louis"
}

# Encounter / battle scripts (3730–4150)
ENCOUNTER_LABELS = {
    3730: "File 3730", 3731: "File 3731", 3732: "File 3732",
    3733: "File 3733", 3734: "File 3734", 3735: "File 3735",
    3736: "File 3736", 3737: "File 3737", 3738: "File 3738",
    3739: "File 3739", 3740: "File 3740", 3741: "File 3741",
    3742: "File 3742", 3743: "File 3743", 3744: "File 3744",
    3745: "File 3745", 3746: "File 3746", 3747: "File 3747",
    3748: "File 3748", 3749: "File 3749", 3750: "File 3750",
    3751: "File 3751", 3752: "File 3752", 3753: "File 3753",
    3754: "File 3754", 3755: "File 3755", 3756: "File 3756",
    3757: "File 3757", 3758: "File 3758", 3759: "File 3759",
    3760: "File 3760", 3761: "File 3761", 3762: "File 3762",
    3763: "File 3763", 3764: "File 3764", 3765: "File 3765",
    3766: "File 3766", 3767: "File 3767", 3768: "File 3768",
    3769: "File 3769", 3770: "File 3770", 3771: "File 3771",
    3772: "File 3772", 3773: "File 3773", 3774: "File 3774",
    3775: "File 3775", 3776: "File 3776", 3777: "File 3777",
    3778: "File 3778", 3779: "File 3779", 3780: "File 3780",
    3781: "File 3781", 3782: "File 3782", 3783: "File 3783",
    3784: "File 3784", 3785: "File 3785", 3786: "File 3786",
    3787: "File 3787", 3788: "File 3788", 3789: "File 3789",
    3790: "File 3790", 3791: "File 3791", 3792: "File 3792",
    3793: "File 3793", 3794: "File 3794", 3795: "File 3795",
    3796: "File 3796", 3797: "File 3797", 3798: "File 3798",
    3799: "File 3799", 3800: "File 3800", 3801: "File 3801",
    3802: "File 3802", 3803: "File 3803", 3804: "File 3804",
    3805: "File 3805", 3806: "File 3806", 3807: "File 3807",
    3808: "File 3808", 3809: "File 3809", 3810: "File 3810",
    3811: "File 3811", 3812: "File 3812", 3813: "File 3813",
    3814: "File 3814", 3815: "File 3815", 3816: "File 3816",
    3817: "File 3817", 3818: "File 3818", 3819: "File 3819",
    3820: "File 3820", 3821: "File 3821", 3822: "File 3822",
    3823: "File 3823", 3824: "File 3824", 3825: "Shared Battle Effect 3825",
    3826: "Shared Battle Effect 3826", 3827: "Shared Battle Effect 3827",
    3828: "Shared Battle Effect 3828", 3829: "Shared Battle Effect 3829",
    3830: "Shared Battle Effect 3830", 3831: "Shared Battle Effect 3831",
    3832: "Shared Battle Effect 3832", 3833: "Shared Battle Effect 3833",
    3834: "Shared Battle Effect 3834", 3835: "Shared Battle Effect 3835",
    3836: "Shared Battle Effect 3836", 3837: "Shared Battle Effect 3837",
    3838: "Shared Battle Effect 3838", 3839: "Shared Battle Effect 3839",
    3840: "Shared Battle Effect 3840", 3841: "Shared Battle Effect 3841",
    3842: "Shared Battle Effect 3842", 3843: "Shared Battle Effect 3843",
    3844: "Shared Battle Effect 3844", 3845: "Shared Battle Effect 3845",
    3846: "Shared Battle Effect 3846", 3847: "Shared Battle Effect 3847",
    3848: "Shared Battle Effect 3848", 3849: "Shared Battle Effect 3849",
    3850: "Shared Battle Effect 3850", 3851: "Shared Battle Effect 3851",
    3852: "Shared Battle Effect 3852", 3853: "Shared Battle Effect 3853",
    3854: "Shared Battle Effect 3854", 3855: "Shared Battle Effect 3855",
    3856: "Shared Battle Effect 3856", 3857: "Shared Battle Effect 3857",
    3858: "Shared Battle Effect 3858", 3859: "Shared Battle Effect 3859",
    3860: "Shared Battle Effect 3860", 3861: "Shared Battle Effect 3861",
    3862: "Shared Battle Effect 3862", 3863: "Shared Battle Effect 3863",
    3864: "Shared Battle Effect 3864", 3865: "Shared Battle Effect 3865",
    3866: "Shared Battle Effect 3866", 3867: "Shared Battle Effect 3867",
    3868: "Shared Battle Effect 3868", 3869: "Shared Battle Effect 3869",
    3870: "Shared Battle Effect 3870", 3871: "Shared Battle Effect 3871",
    3872: "Shared Battle Effect 3872", 3873: "Shared Battle Effect 3873",
    3874: "Shared Battle Effect 3874", 3875: "Shared Battle Effect 3875",
    3876: "Shared Battle Effect 3876", 3877: "Shared Battle Effect 3877",
    3878: "Shared Battle Effect 3878", 3879: "Shared Battle Effect 3879",
    3880: "Shared Battle Effect 3880", 3881: "Shared Battle Effect 3881",
    3882: "Shared Battle Effect 3882", 3883: "Shared Battle Effect 3883",
    3884: "Shared Battle Effect 3884", 3885: "Shared Battle Effect 3885",
    3886: "Shared Battle Effect 3886", 3887: "Shared Battle Effect 3887",
    3888: "Shared Battle Effect 3888", 3889: "Shared Battle Effect 3889",
    3890: "Shared Battle Effect 3890", 3891: "Shared Battle Effect 3891",
    3892: "Shared Battle Effect 3892", 3893: "Shared Battle Effect 3893",
    3894: "Shared Battle Effect 3894", 3895: "Shared Battle Effect 3895",
    3896: "Shared Battle Effect 3896", 3897: "Shared Battle Effect 3897",
    3898: "Enc Storeroom", 3899: "Shared Battle Effect 3899",
    3900: "Shared Battle Effect 3900", 3901: "Shared Battle Effect 3901",
    3902: "Shared Battle Effect 3902", 3903: "Shared Battle Effect 3903",
    3904: "Shared Battle Effect 3904", 3905: "Shared Battle Effect 3905",
    3906: "Shared Battle Effect 3906", 3907: "Shared Battle Effect 3907",
    3908: "Shared Battle Effect 3908", 3909: "Shared Battle Effect 3909",
    3910: "Shared Battle Effect 3910", 3911: "Shared Battle Effect 3911",
    3912: "Shared Battle Effect 3912", 3913: "Shared Battle Effect 3913",
    3914: "Shared Battle Effect 3914", 3915: "Shared Battle Effect 3915",
    3916: "Shared Battle Effect 3916", 3917: "Shared Battle Effect 3917",
    3918: "Shared Battle Effect 3918", 3919: "Shared Battle Effect 3919",
    3920: "Shared Battle Effect 3920", 3921: "Shared Battle Effect 3921",
    3922: "Shared Battle Effect 3922", 3923: "Shared Battle Effect 3923",
    3924: "Shared Battle Effect 3924", 3925: "Shared Battle Effect 3925",
    3926: "Shared Battle Effect 3926", 3927: "Shared Battle Effect 3927",
    3928: "Shared Battle Effect 3928", 3929: "Shared Battle Effect 3929",
    3930: "Shared Battle Effect 3930", 3931: "Shared Battle Effect 3931",
    3932: "Shared Battle Effect 3932", 3933: "Shared Battle Effect 3933",
    3934: "Shared Battle Effect 3934", 3935: "Shared Battle Effect 3935",
    3936: "Shared Battle Effect 3936", 3937: "Shared Battle Effect 3937",
    3938: "Shared Battle Effect 3938", 3939: "Shared Battle Effect 3939",
    3940: "Shared Battle Effect 3940", 3941: "Shared Battle Effect 3941",
    3942: "Shared Battle Effect 3942", 3943: "Shared Battle Effect 3943",
    3944: "Shared Battle Effect 3944", 3945: "Shared Battle Effect 3945",
    3946: "Shared Battle Effect 3946", 3947: "Shared Battle Effect 3947",
    3948: "Shared Battle Effect 3948", 3949: "Shared Battle Effect 3949",
    3950: "Shared Battle Effect 3950", 3951: "Shared Battle Effect 3951",
    3952: "Shared Battle Effect 3952", 3953: "Shared Battle Effect 3953",
    3954: "Shared Battle Effect 3954", 3955: "Shared Battle Effect 3955",
    3956: "Shared Battle Effect 3956", 3957: "Shared Battle Effect 3957",
    3958: "Shared Battle Effect 3958", 3959: "Shared Battle Effect 3959",
    3960: "Shared Battle Effect 3960", 3961: "Shared Battle Effect 3961",
    3962: "Shared Battle Effect 3962", 3963: "Shared Battle Effect 3963",
    3964: "Shared Battle Effect 3964", 3965: "Shared Battle Effect 3965",
    3966: "Shared Battle Effect 3966", 3967: "Shared Battle Effect 3967",
    3968: "Shared Battle Effect 3968", 3969: "Shared Battle Effect 3969",
    3970: "Shared Battle Effect 3970", 3971: "Shared Battle Effect 3971",
    3972: "Shared Battle Effect 3972", 3973: "Shared Battle Effect 3973",
    3974: "Shared Battle Effect 3974", 3975: "Shared Battle Effect 3975",
    3976: "Shared Battle Effect 3976", 3977: "Shared Battle Effect 3977",
    3978: "Shared Battle Effect 3978", 3979: "Shared Battle Effect 3979",
    3980: "Shared Battle Effect 3980", 3981: "Shared Battle Effect 3981",
    3982: "Shared Battle Effect 3982", 3983: "Shared Battle Effect 3983",
    3984: "Shared Battle Effect 3984", 3985: "Shared Battle Effect 3985",
    3986: "Shared Battle Effect 3986", 3987: "Shared Battle Effect 3987",
    3988: "Shared Battle Effect 3988", 3989: "Shared Battle Effect 3989",
    3990: "Shared Battle Effect 3990", 3991: "Shared Battle Effect 3991",
    3992: "Shared Battle Effect 3992", 3993: "Shared Battle Effect 3993",
    3994: "Shared Battle Effect 3994", 3995: "Shared Battle Effect 3995",
    3996: "Shared Battle Effect 3996", 3997: "Shared Battle Effect 3997",
    3998: "Shared Battle Effect 3998", 3999: "Shared Battle Effect 3999",
    4000: "Shared Battle Effect 4000", 4001: "Shared Battle Effect 4001",
    4002: "Shared Battle Effect 4002", 4003: "Shared Battle Effect 4003",
    4004: "Shared Battle Effect 4004", 4005: "Shared Battle Effect 4005",
    4006: "Shared Battle Effect 4006", 4007: "Shared Battle Effect 4007",
    4008: "Shared Battle Effect 4008", 4009: "Shared Battle Effect 4009",
    4010: "Shared Battle Effect 4010", 4011: "Shared Battle Effect 4011",
    4012: "Shared Battle Effect 4012", 4013: "Shared Battle Effect 4013",
    4014: "Shared Battle Effect 4014", 4015: "Shared Battle Effect 4015",
    4016: "Shared Battle Effect 4016", 4017: "Shared Battle Effect 4017",
    4018: "Shared Battle Effect 4018", 4019: "Shared Battle Effect 4019",
    4020: "Shared Battle Effect 4020", 4021: "Shared Battle Effect 4021",
    4022: "Shared Battle Effect 4022", 4023: "Shared Battle Effect 4023",
    4024: "Shared Battle Effect 4024", 4025: "Shared Battle Effect 4025",
    4026: "Shared Battle Effect 4026", 4027: "Shared Battle Effect 4027",
    4028: "Shared Battle Effect 4028", 4029: "Shared Battle Effect 4029",
    4030: "Shared Battle Effect 4030", 4031: "Shared Battle Effect 4031",
    4032: "Shared Battle Effect 4032", 4033: "Shared Battle Effect 4033",
    4034: "Shared Battle Effect 4034", 4035: "Shared Battle Effect 4035",
    4036: "Shared Battle Effect 4036", 4037: "Shared Battle Effect 4037",
    4038: "Shared Battle Effect 4038", 4039: "Shared Battle Effect 4039",
    4040: "Shared Battle Effect 4040", 4041: "Shared Battle Effect 4041",
    4042: "Shared Battle Effect 4042", 4043: "Shared Battle Effect 4043",
    4044: "Shared Battle Effect 4044", 4045: "Shared Battle Effect 4045",
    4046: "Shared Battle Effect 4046", 4047: "Shared Battle Effect 4047",
    4048: "Shared Battle Effect 4048", 4049: "Shared Battle Effect 4049",
    4050: "Shared Battle Effect 4050", 4051: "Shared Battle Effect 4051",
    4052: "Shared Battle Effect 4052", 4053: "Shared Battle Effect 4053",
    4054: "Shared Battle Effect 4054", 4055: "Shared Battle Effect 4055",
    4056: "Shared Battle Effect 4056", 4057: "Shared Battle Effect 4057",
    4058: "Shared Battle Effect 4058", 4059: "Shared Battle Effect 4059",
    4060: "Shared Battle Effect 4060", 4061: "Shared Battle Effect 4061",
    4062: "Shared Battle Effect 4062", 4063: "Shared Battle Effect 4063",
    4064: "Shared Battle Effect 4064", 4065: "Shared Battle Effect 4065",
    4066: "Shared Battle Effect 4066", 4067: "Shared Battle Effect 4067",
    4068: "Shared Battle Effect 4068", 4069: "Shared Battle Effect 4069",
    4070: "Shared Battle Effect 4070", 4071: "Shared Battle Effect 4071",
    4072: "Shared Battle Effect 4072", 4073: "Shared Battle Effect 4073",
    4074: "Shared Battle Effect 4074", 4075: "Shared Battle Effect 4075",
    4076: "Shared Battle Effect 4076", 4077: "Shared Battle Effect 4077",
    4078: "Shared Battle Effect 4078", 4079: "Shared Battle Effect 4079",
    4080: "Shared Battle Effect 4080", 4081: "Shared Battle Effect 4081",
    4082: "Shared Battle Effect 4082", 4083: "Shared Battle Effect 4083",
    4084: "Shared Battle Effect 4084", 4085: "Shared Battle Effect 4085",
    4086: "Shared Battle Effect 4086", 4087: "Shared Battle Effect 4087",
    4088: "Shared Battle Effect 4088", 4089: "Shared Battle Effect 4089",
    4090: "Shared Battle Effect 4090", 4091: "Shared Battle Effect 4091",
    4092: "Shared Battle Effect 4092", 4093: "Shared Battle Effect 4093",
    4094: "Shared Battle Effect 4094", 4095: "Shared Battle Effect 4095",
    4096: "Shared Battle Effect 4096", 4097: "Shared Battle Effect 4097",
    4098: "Shared Battle Effect 4098", 4099: "Shared Battle Effect 4099",
    4100: "Shared Battle Effect 4100", 4101: "Shared Battle Effect 4101",
    4102: "Shared Battle Effect 4102", 4103: "Shared Battle Effect 4103",
    4104: "Shared Battle Effect 4104", 4105: "Shared Battle Effect 4105",
    4106: "Shared Battle Effect 4106", 4107: "Shared Battle Effect 4107",
    4108: "Shared Battle Effect 4108", 4109: "Shared Battle Effect 4109",
    4110: "Shared Battle Effect 4110", 4111: "Shared Battle Effect 4111",
    4112: "Shared Battle Effect 4112", 4113: "Shared Battle Effect 4113",
    4114: "Shared Battle Effect 4114", 4115: "Shared Battle Effect 4115",
    4116: "Shared Battle Effect 4116", 4117: "Shared Battle Effect 4117",
    4118: "Shared Battle Effect 4118", 4119: "Shared Battle Effect 4119",
    4120: "Shared Battle Effect 4120", 4121: "Shared Battle Effect 4121",
    4122: "Shared Battle Effect 4122", 4123: "Shared Battle Effect 4123",
    4124: "Shared Battle Effect 4124", 4125: "Shared Battle Effect 4125",
    4126: "Shared Battle Effect 4126", 4127: "Shared Battle Effect 4127",
    4128: "Shared Battle Effect 4128", 4129: "Shared Battle Effect 4129",
    4130: "Shared Battle Effect 4130", 4131: "Shared Battle Effect 4131",
    4132: "Shared Battle Effect 4132", 4133: "Shared Battle Effect 4133",
    4134: "Shared Battle Effect 4134", 4135: "Shared Battle Effect 4135",
    4136: "Shared Battle Effect 4136", 4137: "Shared Battle Effect 4137",
    4138: "Shared Battle Effect 4138", 4139: "Shared Battle Effect 4139",
    4140: "Shared Battle Effect 4140", 4141: "Shared Battle Effect 4141",
    4142: "Shared Battle Effect 4142", 4143: "Shared Battle Effect 4143",
    4144: "Enc Storeroom", 4145: "Shared Battle Effect 4145",
    4146: "Shared Battle Effect 4146", 4147: "Shared Battle Effect 4147",
    4148: "Shared Battle Effect 4148", 4149: "Shared Battle Effect 4149",
    4150: "Shared Battle Effect 4150",
}


@lru_cache(maxsize=1)
def generate_name_overrides() -> dict[int, str]:
    """Build complete index→label map with category prefixes. Result is cached; callers must not mutate it."""
    overrides = {}

    for idx, name in SYSTEM_LABELS.items():
        overrides[idx] = name
    for idx, name in AREA_LABELS.items():
        overrides[idx] = name
    for idx, name in CHARACTER_LABELS.items():
        overrides[idx] = name
    for idx, name in MONSTER_LABELS.items():
        overrides[idx] = name
    for idx, name in PROP_LABELS.items():
        overrides[idx] = name
    for idx, name in EQUIP_LABELS.items():
        overrides[idx] = name
    for idx, name in VFX_LABELS.items():
        overrides[idx] = name
    for idx, name in ANIMATION_LABELS.items():
        overrides[idx] = name
    overrides.update(SCENE_GROUP_LABELS)
    overrides.update(ENCOUNTER_LABELS)

    return overrides

class DatacenterTargets:
    '''Kods datacenter targets'''
    _TARGET_STATIC: dict[int, tuple[int, ...]] = { # format: [disk index]:[Datacenter header HIDs]
        186: (5, 0),  204: (5, 1),
        185: (5, 7),  187: (5, 9),
        203: (5, 10), 189: (5, 11), 190: (5, 12),
        191: (5, 13), 192: (5, 14), 193: (5, 15),
        194: (5, 16), 195: (5, 17), 196: (5, 18),
        197: (5, 19), 198: (5, 20), 199: (5, 21),
        200: (5, 22), 201: (5, 23), 206: (5, 25),
        179: (5, 26), 178: (5, 27), 177: (5, 28),
        176: (5, 29), 180: (5, 30), 188: (5, 31)
    }
    _TARGET_MAP: list[tuple[int, int, tuple[int, ...], int]] = [ # format: [Start Idx, End Idx, HID prefix, Number of Header Idxs]
        (1206, 1511, (5, 2, 0), 10),
        (1511, 1682, (5, 3, 0), 10),
        (1688, 1939, (5, 4, 0), 10),
        (1939, 2126, (5, 5, 0), 10),
        (2126, 2426, (5, 6, 0), 10),
    ]
    @classmethod
    def get_target(
        cls,
        disk_index: int,
        child_index: int | None = None,
        ) -> list[tuple[int,...]] | None:
        '''Return the datacenter header HID(s) for a node HID where,
        disk index is the top level HID value and child_index is 0-8 for entity sections'''
        # Single datacenter header
        if disk_index in cls._TARGET_STATIC:
            if child_index is not None:
                return None
            return [cls._TARGET_STATIC[disk_index]]

        # Entity Pack datacenter headers
        for start, end, prefix, steps in cls._TARGET_MAP:
            if start <= disk_index < end:
                base_val = (disk_index - start) * steps
                if child_index is None:
                    return [prefix + (base_val,)]
                if 0 <= child_index < steps - 1:
                    return [prefix + (base_val + 1 + child_index,)]
                return None
        return None

    @classmethod
    def to_hid_str_map(cls):
        '''Used to export and repopulate metadata with target_hids
        yields hid string representations mapped to flat integer tuples'''
        for disk_idx, hid in cls._TARGET_STATIC.items():
            yield str(disk_idx), hid
        for start, end, prefix, steps in cls._TARGET_MAP:
            for disk_idx in range(start, end):
                base = (disk_idx - start) * steps
                yield str(disk_idx), prefix + (base,)
                for child_idx in range(steps -1):
                    yield f'{disk_idx}.{child_idx}', prefix + (base + 1 + child_idx,)

class PhysicalFileCategories:
    _CATEGORIES = {
        range(8, 17):       ('FMV',),
        range(3, 4):        ('TAC', 'Audio',),
        range(42, 176):     ('TAC', 'Audio',),
        range(176, 181):    ('TAC', 'Audio',),
        range(188, 189):    ('TAC', 'Audio',),
        range(184, 188):    ('Script',),
        range(204, 205):    ('Script',),
        range(206, 207):    ('Script',),
        range(4, 5):        ('Texture',),
        range(26, 27):      ('Texture',),
        range(189, 204):    ('Texture',),
        range(205, 206):    ('Texture',),
        range(207, 1206):   ('Map',),
        range(1206, 1511):  ('Character',),
        range(1511, 1688):  ('Monster',),
        range(1688, 1939):  ('Prop',),
        range(1939, 2126):  ('Equipment',),
        range(2126, 2426):  ('VFX',),
        range(2426, 3426):  ('Scene Setup',),
        range(3426, 3629):  ('Animation',),
        range(3730, 4151):  ('Battle Animation',),

        range(0, 3):        ('System',), # boot
        range(5, 8):        ('System',), # datacenter/SO3
        range(18, 26):      ('System',), # stats
        range(27, 42):      ('System',), # core/debug
        range(182, 184):    ('System',), # game
    }
    @classmethod
    def iter_entries(cls) -> Iterator[tuple[str, dict[str, Any]]]:
        for idx_range, category in cls._CATEGORIES.items():
            for disk_idx in idx_range:
                yield f'{disk_idx}', {'tags': category}

class PhysicalFileNames:
    @classmethod
    def iter_entries(cls) -> Iterator[tuple[str, dict[str, Any]]]:
        _NAMES: dict[int, str] = generate_name_overrides()
        for entry in _NAMES.items():
            yield f'{entry[0]}', {'title': entry[1]}

class EntityPackSections:
    _SECTIONS = {
        0: ['Model Data', ('Model',)],
        1: ['Basic Animation Data', ('Animation',)],
        2: ['Secondary Basic Animation Data', ('Animation',)],
        3: ['Battle Data', ('Battle',)],
        4: ['Secondary Battle Data', ('Battle',)],
        5: ['Script Animation Data', ('Script', 'Animation')],
        6: ['Animation Data 6', ('Animation',)],
        7: ['Script Data', ('Script',)],
        8: ['NPC Battle Data', ('Battle',)]
    }
    _RANGE = range(1207, 2425)

    @classmethod
    def iter_entries(cls) -> Iterator[tuple[str, dict[str, Any]]]:
        for disk_idx in cls._RANGE:
            for child_idx, values in cls._SECTIONS.items():
                yield f'{disk_idx}.{child_idx}', {'title': values[0], 'tags': values[1]}

class MapSections:
    '''Auto-fill map packs with section metadata'''
    _SECTIONS = {
        1: ['Object References', ('System', 'Map')],
        2: ['Script Data', ('Script', 'Map')],
        3: ['Model Data', ('Model', 'Map')],
        4: ['Animation Data', ('Animation', 'Map')],
        5: ['Terrain Data', ('Terrain', 'Map')],
        6: ['Message Data', ('Message', 'Map')],
        8: ['Scene Graph', ('Scene Graph', 'Map')],
        9: ['Music Data', ('Audio', 'Map')]
    }
    _RANGE = range(207, 1206)
    @classmethod
    def iter_entries(cls) -> Iterator[tuple[str, dict[str, Any]]]:
        for disk_idx in cls._RANGE:
            for child_idx, values in cls._SECTIONS.items():
                yield f'{disk_idx}.{child_idx}', {'title': values[0], 'tags': values[1]}

class IOPModules:
    _SECTIONS = {
        0: ['sio2man', 'Manager Interface for joypads, multitaps and memory cards.',],
        1: ['sio1d', 'Interface for joypads, multitaps and memory cards.',],
        2: ['dbcman', 'Device Control Library (used by libpad2 and libmc2)',],
        3: ['ds1o_d', '',],
        4: ['libsd', 'Sound Library',],
        5: ['csm', '',],
        6: ['csd', '',],
        7: ['csi', '',],
        8: ['hdd', 'Hard Disk Drive',],
        9: ['pfs', 'Playstation File System',],
        10: ['mcman', 'MCMAN is the memory card manager',],
        11: ['mcserv', 'MCSERV is the memory card server. This provides the RPC interface to MCMAN',]
    }
    _RANGE = range(1, 2)
    @classmethod
    def iter_entries(cls) -> Iterator[tuple[str, dict[str, Any]]]:
        for disk_idx in cls._RANGE:
            for child_idx, values in cls._SECTIONS.items():
                fields: dict[str, Any] = {'title': values[0]}
                if len(values) > 1 and values[1]:
                    fields['description'] = values[1]
                fields['tags'] = ('System', 'IOP')
                fields['extension'] = '.IRX'
                yield f'{disk_idx}.{child_idx}', fields

class EventScripts:
    _EVENTS = {
        '400':    ['Goblin Trio @ Earth Valley'],
        '400.1':  ['Goblin Trio at the Pub'],
        '401':    ['Clive @ Theatre Vancoor'],
        '401.1':  ["Introduction to friends list and Clive's recruitment"],
        '402':    ['Fayt Armor'],
        '402.1':  ["Enter Ridley's room after becoming a knight again"],
        '403':    ['Goblin Cemetary Mission (Theatre Vancoor)'],
        '403.1':  ['Goblin Encyclopedia @ Vareth'],
        '403.2':  ['Before boss battle'],
        '403.3':  ['Arrive at Goblin Cemetary Entrance'],
        '403.4':  ['After boss battle'],
        '403.5':  ['After obtaining Recruitment Suit'],
        '404':   ['Algandars Castle (Theatre Vancoor - Human Path)'],
        '404.1': ['First meeting at Algandars Castle entrance'],
        '404.2': ['Before boss battle'],
        '404.3': ['Meeting after boss battle at entrance'],
        '404.4': ['After boss battle'],
        '405':   ['Creatures of the Sewer'],
        '405.1': ['Pre battle'],
        '405.2': ['Entering Sewer'],
        '405.3': ['Entering room prior to boss'],
        '405.4': ['Post Battle'],
        '406':   ['Chains of Fate (Unused Version)'],
        '406.1': ['Talking to Nocturne'],
        '406.2': ['Talking to Gerald'],
        '407':   ['Fireworks'],
        '407.1': ['Getting the letter'],
        '407.2': ['Fireworks event'],
        '408':   ['Stone of Miracles'],
        '408.1': ['Pre Battle'],
        '408.2': ['Post Battle'],
        '408.3': ['Kain telling Jack where to find'],
        '408.4': ['Bringing Stone to Kain'],
        '409':   ['Please Stop Lord Star'],
        '409.1': ['Pre Battle'],
        '409.2': ['Post Battle'],
        '410':   ['The Ultimate Battle'],
        '410.1': ['Pre Battle'],
        '410.2': ['Post Battle'],
        '410.3': ['Elwen telling Jack to do mission'],
        '411':   ['The Real Ultimate Battle'],
        '411.1': ['Pre Battle'],
        '411.2': ['Post Battle'],
        '412':   ["Gonovitch's Dilemma"],
        '412.1': ['Pre Battle'],
        '412.2': ['Post Battle'],
        '412.3': ['Gonovitch Pre'],
        '412.4': ['Gonovitch Post'],
        '413':   ['Earth Dragon Encounter at Dwarf Tunnel (Unused)'],
        '413.1': ['First time entering room'],
        '413.2': ['Pre Battle'],
        '413.3': ['Post Battle'],
        '414':   ["Hecton Squad's Lunch Plans"],
        '414.1': ['Start'],
        '414.2': ['Win Battle'],
        '414.3': ['Lose Battle'],
        '415':   ['Encounter with Leona at Vareth'],
        '416':   ['Post-Game Dungeon'],
        '416.1': ['First time entering DLC'],
        '416.2': ['Pre Battle - Baade (Earth Dragon)'],
        '416.3': ['Post Battle - Baade'],
        '416.4': ['Pre Battle - Kelvin (Water Dragon)'],
        '416.5': ['Post Battle - Kelvin'],
        '416.6': ['Pre Battle - Parsec (Fire Dragon)'],
        '416.7': ['Post Battle - Parsec'],
        '416.8': ['Pre Battle - Cepheid (Wind Dragon)'],
        '416.9': ['Post Battle - Cepheid'],
        '416.10': ['Unlock Radian'],
        '416.11': ['Pre Battle - Radian'],
        '416.12': ['Post Battle - Radian'],
        '416.13': ['First time entering Distortion Corridor / unlocking scene'],
        '416.14': ['Pre Battle - Cairn'],
        '416.15': ['Post Battle - Cairn'],
        '416.16': ['Pre Battle - Valkyrie'],
        '416.17': ['Post Battle - Valkyrie'],
        '416.18': ['Pre Battle - Quasar'],
        '416.19': ['Post Battle - Quasar'],
        '416.20': ['Wall Text'],
        '416.21': ['Pre Battle - Lezard'],
        '416.22': ['Post Battle - Lezard'],
        '416.23': ['Pre Battle - Gabriel Celesta'],
        '416.24': ['Post Battle - Gabriel Celesta'],
        '416.25': ['Pre Battle - Ethereal Queen'],
        '416.26': ['Post Battle - Ethereal Queen'],
        '416.27': ['Wind Dragon - Phase 2'],
        '416.28': ['Teleport from DLC to Distortion'],
        '417':   ['Gawain gives Jack the Arbitrator'],
        '417.1': ['Pre Battle'],
        '417.2': ['Post Battle'],
        '418':   ['Elwen gives Jack the Arbitrator (Unused)'],
        '419':   ["Leonard's Letter (Knights)"],
        '420':   ['Goblin Cemetery Mission (Non-Human Path)'],
        '421':   ['The Best Liquor (Liquor Fetch Quest - Non-Human Path)'],
        '422':   ['Letter of Defiance'],
        '423':   ['A Masterpiece of Fantasy'],
        '424':   ['Algandars Castle (Non-Human)'],
        '425':   ['Beasts by the Bridge'],
        '426': ["Practicing Business (Keane and Marsha's Shop Event)"],
        '427': ['Build that Body'],
        '428': ['Top Secret Mission'],
        '429': ['Shining Ore'],
        '430': ['J.J. and Galvados Event'],
        '431': ["Elven Wine for Ganz"],
        '432': ['Chains of Fate'],
        '433': ['The Strongest Elf (Franz & Gil)'],
        '434': ['Journey Pig Introduction'],
        '435':   ['Jack vs Hecton Squad (Non-Human)'],
        '435.1': ['Start'],
        '435.2': ['Win Battle'],
        '435.3': ['Lose Battle'],
        '436':   ['Jack vs Elwen (Non-Human)'],
        '436.1': ['Start'],
        '436.2': ['Win Battle'],
        '436.3': ['Lose Battle'],
        '437':   ['Jack vs Gerald (Non-Human)'],
        '437.1': ['Start'],
        '437.2': ['Win Battle'],
        '437.3': ['Lose Battle'],
        '500':   ['Start of game > Departure for first mission'],
        '500.1': ['Starting FMV / Coliseum Waiting Room'],
        '500.2': ['Pre Ridley Battle'],
        '500.3': ['Post Ridley Battle'],
        '500.4': ["Al showing Jack his room"],
        '500.5': ['Jack gets Trainee Wear'],
        '500.6': ['Knocking on Door to Meeting Room'],
        '500.7': ['Rose Cochon Inauguration Ceremony'],
        '500.8': ['Rose Cochon leaves for First Mission'],
        '500.9': ['Jasne talking to Natalie'],
        '500.10': ['Rose Cochon meets Clive'],
        '500.11': ['Movement Restriction Message'],
        '500.12': ['Movement Restriction Message'],
        '500.13': ['Adele and Jack - First cutscene'],
        '500.14': ['Adele and Jack Training'],
        '500.15': ['Adele gives Jack the Arbitrator'],
        '500.16': ['Jack walking to Radiata'],
        '500.17': ['Jack at Radiata Castle'],
        '500.18': ['Save Flag Tutorial'],
        '501':   ['First Mission (Outside Lupus Gate) > End of First Mission'],
        '501.1': ['Ganz: "Now then. Here are the details of our mission."'],
        '501.2': ['Jack: "Far out! Dwarves live in a crazy place like this?"'],
        '501.3': ['Jack: "Open up!"'],
        '501.4': ['Gonovitch: "So you are here instead of the Violet Chevre."'],
        '501.5': ['Ganz: "It seems that the goods are ready. Let\'s head to the top of the cliff."'],
        '501.6': ['Goblin Trio - Pre Battle'],
        '501.7': ['Goblin Trio - Post Battle'],
        '501.8': ['Ganz: "Ah, at last. Radiata Castle."'],
        '501.9': ['Ganz: "Brigade, halt!"'],
        '501.10': ['Larks: "I\'m glad you\'ve returned safely."'],
        '501.11': ['Movement Restriction Message'],
        '501.12': ['Movement Restriction Message'],
        '501.13': ['Movement Restriction Message'],
        '501.14': ['Movement Restriction Message'],
        '501.15': ['Movement Restriction Message'],
        '501.16': ['Ganz: "We are the Rose Cochon brigade. We are here to escort the trade goods in place of the Violet Chevre.'],
        '501.17': ['Movement Restriction Message'],
        '501.18': ['Movement Restriction Message'],
        '501.19': ['Movement Restriction Message'],
        '501.20': ['Movement Restriction Message'],
        '501.21': ['Gonovitch: "Come in! I\'m on the second floor."'],
        '501.22': ['Jack: "So, what do we do now that we\'ve finished the mission?"'],
        '501.23': ['Ganz: "Captain Ganz Rothschild and the Rose Cochon brigade reporting, sir!"'],
        '501.24': ['Ganz: "As Lord Larks said, it is very important that knights rest in preparation for their next mission."'],
        '502':   ['Second Mission (Knights)'],
        '502.1': ['Al tells Jack of emergency summons'],
        '502.2': ['Jack at meeting room'],
        '502.3': ['Fort Helencia'],
        '502.4': ['Natalie and Leonard outside Fort'],
        '502.5': ['Meeting Genius'],
        '502.6': ['Nogueira kills Blood Orc'],
        '502.7': ['First Arrival at City of Flowers'],
        '502.8': ['Jack praises Ganz'],
        '502.9': ['Nowem Region Appreciation'],
        '502.10': ['Arrival at Forest Metropolis'],
        '502.11': ['Meeting Lord Nogueira'],
        '502.12': ['Movement Restriction'],
        '502.13': ['Finding out about Blood Orc'],
        '502.14': ['Blood Orc'],
        '502.15': ['Transpiritation'],
        '502.16': ['Rose Cochon arrives at Castle Gate (Unused)'],
        '502.17': ['Rose Cochon and Jasne'],
        '502.18': ['Movement Restriction'],
        '502.19': ['Movement Restriction'],
        '502.20': ['Movement Restriction'],
        '502.21': ['Movement Restriction'],
        '502.22': ['Movement Restriction'],
        '502.23': ['Movement Restriction'],
        '502.24': ['Movement Restriction'],
        '502.25': ['Movement Restriction'],
        '502.26': ['Movement Restriction'],
        '502.27': ['Movement Restriction'],
        '502.28': ['Meeting Rocky (1) (Unused)'],
        '502.29': ['Meeting Rocky (2) (Unused)'],
        '502.30': ['After first Blood Orc Battle'],
        '503':   ['Radiata Castle Dungeon Event'],
        '504':   ["Lucian's Scheme (Unused)"],
        '505':   ['Rose Cochon Discharged'],
        '506':   ["Carl's Pub > Jack becoming Corporal"],
        '507':   ['Vancoor Square (Sheila Event)'],
        '508':   ['Crocogator Mission'],
        '509':   ['Smilodon Fang'],
        '510':   ['Vexatious Vermin'],
        '511':   ['Parsec at Vancoor Square (Unused)'],
        '512':   ["Ridley's Birthday, Graveyard of the Elves"],
        '513':   ["Second Hecton Squad Mission > Jack becomes a Sergeant"],
        '514':   ["Knight's discussing Dwarves' demands > Cross' Invasion of Earth Valley"],
        '515':   ['Donovitch appears > Earth Dragon'],
        '516':   ["Path Split (Ridley visits Jack)"],
        '516.1': ['Updates Characters with new Items (Shops)'],
        '554':   ["Lucian and Jasne get Rose Cochon Discharged"],
        '561':   ['Parsec at Vancoor Square'],
        '600':   ['Meeting at the Castle'],
        '601':   ['Wind Valley (Wind Dragon) > Gawain'],
        '602':   ['Parsec > Fire Mountain'],
        '603':   ["Lucian's Paintings"],
        '604':   ['Ridley visits Jack'],
        '605':   ["Ganz's Letter > Castle Jailbreak"],
        '606':   ['Gold Dragon at Lupus Gate > Zane'],
        '650':   ['Castle Meeting'],
        '651':   ['Wind Valley (Wind Dragon)'],
        '652':   ['Gawain at Fort Helencia'],
        '653':   ['Dynas makes Jack a Knight Captain'],
        '654':   ['Fire Dragon at Faucon Gate > Fire Mountain'],
        '655':   ['Secrets of the Sewers'],
        '656':   ["Ganz's Letter"],
        '657':   ['Ridley visits Jack'],
        '658':   ['Battle at Lupus Gate'],
        '659':   ['VS Gawain'],
        '660':   ['Gold Dragon Castle'],
        '700':   ['Jack and Ridley leave for City of Flowers'],
        '701':   ['Taking over Fort Helencia'],
        '702':   ['Parsec'],
        '703':   ['Goblin Haven'],
        '704':   ['Parsec (Fire Mountain)'],
        '705':   ["Lucian's Paintings"],
        '706':   ['Cross Attacks Fort Helencia'],
        '707':   ["Ridley's Mind"],
        '708':   ['Ridley Becomes the Gold Dragon'],
        '750':   ['Jack and Ridley go to the City of Flowers'],
        '751':   ['Capturing Fort Helencia'],
        '752':   ['Meeting with Parsec'],
        '753':   ['Goblin Haven'],
        '754':   ["Ridley's Illness"],
        '755':   ['Parsec (Fire Mountain)'],
        '756':   ['Ganz rescues Adele'],
        '757':   ['Cross Attacks the Fort'],
        '758':   ['Ressan Tree'],
        '759':   ['Jack heads to the End of the World'],
        '760':   ['Gold Dragon Castle'],
        '800':   ['Misc'],
        '800.1': ['Midnight Transition Animation'],
        '800.2': ['Starting Data for Characters (Run on New Game start)'],
        '800.3': ['Training Dummy'],
        '800.4': ['Journey Pig Statue'],
        '800.5': ['Save Flag'],
        '800.6': ['Add Missions (Debug Room)'],
        '800.7': ['Run after a training dummy mission (updates reward if won)'],
        '801':   ['Knights Invade Earth Valley FMV (Game Engine)'],
        '802':   ['Debug Room'],
        '803':   ['Transpiritation FMV (Game Engine)'],
        '804':   ['Earth Dragon FMV (Game Engine)'],
        '805':   ['Post-Battle Script'],
        '805.1': ['This is usually run after a kick battle'],
        '806':   ['Silver Dragon FMV (Game Engine)'],
        '807':   ['Movement Restriction Messages'],
        '808':   ['Completed Save Data'],
        '808.1': ['Prompt to save after beating the game'],
        '809':   ['Movement Restriction Messages'],
        '810':   ['Movement Restriction Messages'],
        '811':   ['Tokyo Game Show 2004 / E3 2005 Demo'],
    }
    _RANGE = range(186, 187)
    @classmethod
    def iter_entries(cls) -> Iterator[tuple[str, dict[str, Any]]]:
        for disk_idx in cls._RANGE:
            for sub_idx, values in cls._EVENTS.items():
                base_name = values[0]
                base_key = f'{disk_idx}.{sub_idx}'
                if '.' in sub_idx:
                    yield base_key, {'title': f'{base_name} Compressed', 'tags': ('Script',)}
                    yield f'{base_key}.0', {'title': f'{base_name} Container', 'tags': ('Script',)}
                    yield f'{base_key}.0.0', {'title': f'{base_name} Script Data', 'tags': ('Script',)}
                    yield f'{base_key}.0.1', {'title': f'{base_name} Message Data', 'tags': ('Script',)}
                else:
                    yield base_key, {'title': f'{base_name}', 'tags': ('Script',)}

class CharaPortraits:
    '''Sequential list of all the names for portraits
    Loops twice first time adds the "compressed" suffix second time is basic'''
    _ICONS = [
        'Placeholder Icon', 'Jack Icon', 'Ganz Icon', 'Ridley Icon','Rynka Icon', 'Flau Icon',
        'Star', 'Sebastian', 'Genius', 'Rocky', 'Gawain', 'Heavy Guardsman', 'Elwen', 'Gerald',
        'Caesar', 'Alicia', 'Dennis', 'Gareth', 'Gregory', 'Walter', 'Jarvis', 'Light Guardsman',
        'Aldo', 'Gordon', 'Bruce', 'David', 'Conrad', 'Rolec', 'Daniel', 'Carlos', 'Gene',
        'Light Guardsman', 'Thanos', 'Curtis', 'Cecil', 'Morgan', 'Felix', 'Jill', 'Ursula',
        'Derek', 'Christoph', 'Claudia', 'Ardoph', 'Dimitri', 'Aidan', 'Cornelia', 'Faraus',
        'Marietta', 'Ernest', 'Franklin', 'Johan', 'Roche', 'Light Guardsman', 'Kain', 'Fernando',
        'Anastasia', 'Dwight', 'Godwin', 'Achilles', 'Flora', 'Elena', 'Alvin', 'Vitas', 'Cosmo',
        'Grant', 'Adina', 'Miranda', 'Edgar', 'Clive', 'Lulu', 'Eugene', 'Nyx', 'Ortoroz', 'Sonata',
        'Iris', 'Nocturne', 'Herz', 'Alba', 'Lily', 'Jared', 'Pinky', 'Interlude', 'Solo', 'Joaquel',
        'Eon', 'Elmo', 'Jiorus', 'Sarasenia', 'Belflower', 'Jasne', 'Larks', 'Sakurazaki', 'Junzaburo',
        'Natalie', 'Nina', 'Charlie', 'Leonard', 'Light Guardsman', 'Heavy Guardsman', 'Raymond', 'Al',
        'Margaret', 'Zion', 'Paul', 'Toma', 'Torenia', 'Testa', 'Nuse', 'Jorn', 'Barbena', 'Giske',
        'Yuri', 'Warc', 'Robin', 'Sheila', 'Jasmine', 'Camuse', 'Lantana', 'Lyle', 'Rose', 'Josef',
        'Virginia', 'Morfinn', 'Bligh', 'Freija', 'Nask', 'Cherie', 'Zeke', 'Dan', 'Servia', 'Lunbar',
        'Sonia', 'Startis', 'Brood', 'Garbella', 'Silvia', 'Thyme', 'Elef', 'Ryan', 'Hip', 'Nick', 'Kira',
        'Rabi', 'Golye', 'Butch', 'Sarval', 'Sunset', 'Sora', 'Keaton', 'Tarkin', 'Gonber', 'Leban', 'Mook',
        'Wal', 'Wyze', 'Zeranium', '', 'Pommelie', 'Saron', 'Cepheid', 'Baade', 'Quasar', 'Aphelion',
        'Gonovitch', 'Albert', 'Vladimir', 'Yevgeni', 'Oleg', 'Grigory', 'Brockle', 'Dyvad', 'Gehrmann',
        'Sergei', 'Naom', 'Aegenhart', 'Marke', 'Donovitch', 'Zane', 'Hap', 'Gil', 'Shin', 'Fan', 'Row',
        'Pitt', 'Few', 'Alan', 'Keane', 'Nogueira', 'Clarence', 'Serva', 'Hyann', 'Chatt', 'Zida', 'Franz',
        'Romaria', 'Marsha', 'Lufa', 'Coco', 'Martinez', 'Santos', 'Rika', 'Mikey', 'Gob', 'Lin', 'Brie',
        'Gonn', 'Golly', 'Gobrey', 'Den', 'Ben', 'Aesop', 'Monki', 'Gabe', 'Mason', 'Goo', 'Donkey',
        'Ricky', 'Drew', 'Gruel', 'Doppio', 'Pietro', 'Jan', 'Marco', 'Niko', 'Danny', 'Dominic', 'Bosso',
        'Georgio', 'Luka', 'Sonny', 'Giovanni', 'Polpo', 'Jj', 'Leona', 'Leann', 'Ray C Ross', 'Pinta',
        'Buta', 'Valkyrie', 'Lezard', 'Radian', 'Ethereal Queen', 'Cairn', 'Kelvin', 'Gabriel Celesta',
        '', '', 'Galvados', '', '', '', '', '', 'Drago', 'Bull', '', '', '',
        '', 'Library', 'Phonograph', 'Jack Bookshelf', 'Cross', 'Stein', 'Blackjack', 'Event Watcher',
        'Parsec', 'Light Guardsman', 'Light Guardsman', 'Light Guardsman', 'Heavy Guardsman',
        'Heavy Guardsman', 'Heavy Guardsman', 'Heavy Guardsman', 'Heavy Guardsman', 'Heavy Guardsman',
        'Heavy Guardsman', 'Heavy Guardsman', 'Heavy Guardsman', 'Cody', 'Adele', 'Howard', 'Ravil',
        'Astor', 'Maddock', 'Synelia', 'Tony', 'Patrick', 'Putt', 'Reynos', 'Gobblehope Ix', 'Nalshay',
        'Sayna', 'Bran', 'Stefan', 'Mint', 'Daria', 'Yack', 'Lauren', 'Theresa', 'Garcia', 'Dynas', 'Epoch',
        'Roy', 'Louis',
    ]
    _THOUSANDS = [
        'Jack Handmade Tunic', 'Jack Trainee\'s Wear', 'Jack Leather Armor', 'Jack Sharkskin',
        'Jack Iron Breastplate', 'Jack Wooden Breastplate', 'Jack Wind Garb', 'Jack Divine Coat',
        'Jack Alfestrain', 'Jack Scale Armor', 'Jack Dragon Scale', 'Jack Iron Plate', 'Jack Plate Armour',
        'Jack Ore Armour', 'Jack Valiant Mail', 'Jack Demon Mail', 'Jack Samurai Armour',
        'Jack Absolute Guard', 'Jack Fayt Armour', 'Jack Robot Suit', 'Jack Recruitment Suit',
        'Ganz Second', 'Ridely Second', 'Ridely Third', 'Adele Second', 'Ridely Fourth'
    ]
    _RANGE_ICONS = 205
    _RANGE_BANK03 = 192
    @classmethod
    def iter_entries(cls) -> Iterator[tuple[str, dict[str, Any]]]:
        # Fill 205 with metadata
        disk_idx = cls._RANGE_ICONS
        for child_idx, name in enumerate(cls._ICONS):
            yield f'{disk_idx}.{child_idx}', {'title': f'{name} Icon (Compressed)', 'tags': ('Texture',)}
        for child_idx, name in enumerate(cls._ICONS):
            yield f'{disk_idx}.{child_idx}.0', {'title': f'{name} Icon', 'tags': ('Texture', 'FIS')}
        for child_idx, name in enumerate(cls._THOUSANDS):
            yield f'{disk_idx}.{child_idx+1000}', {'title': f'{name} Icon (Compressed)', 'tags': ('Texture',)}
        for child_idx, name in enumerate(cls._THOUSANDS):
            yield f'{disk_idx}.{child_idx+1000}.0', {'title': f'{name} Icon', 'tags': ('Texture', 'FIS')}
        # Fill Texture Bank 03 with metadata
        disk_idx = cls._RANGE_BANK03
        for child_idx, name in enumerate(cls._ICONS):
            yield f'{disk_idx}.{child_idx}', {'title': f'{name} Portrait (Compressed)', 'tags': ('Texture',)}
        for child_idx, name in enumerate(cls._ICONS):
            yield f'{disk_idx}.{child_idx}.0', {
                'title': f'{name} Portrait',
                'tags': ('Texture', 'FIS'),
                'description': f'Friends book portrait for {name}.'
            }

class TextureBanks:
    _BANK00 = {
        1: {'title': 'Thought Bubbles', 'description': 'Channel packed.'},
        3: {'title': 'Clock'},
        9: {'title': 'Bear Icons'},
        10: {'title': 'Demo Finished Screen 1', 'description': 'First part of the "Hope you enjoyed playing" demo screen.'},
        11: {'title': 'Demo Finished Screen 2', 'description': 'Second part of the "Hope you enjoyed playing" demo screen.'},
        12: {'title': 'Demo Finished Screen 3', 'description': 'Third part of the "Hope you enjoyed playing" demo screen.'},
        20: {'title': 'Horizontal Menu Background 1', 'description': 'The background page-like texture underlay for menus.'},
        21: {'title': 'Vertical Menu Background 1', 'description': 'The background page-like texture underlay for menus.'},
        22: {'title': 'Horizonztal Menu Background 2', 'description': 'The background page-like texture underlay for menus.'},
        23: {'title': 'Vertical Menu Background 2', 'description': 'The background page-like texture underlay for menus.'},
        24: {'title': 'Save Menu & Cursor', 'description': 'Save menu elements and cursor.'},
        25: {'title': 'Totem Icon', 'description': 'Totem Icon as well as unknown japanese text.'},
        26: {'title': 'Unknown Japanese prompt', 'description': 'Contains an unknown japanese menu elements.'},
        27: {'title': 'Jack, Ganz, Ridely Cute Icons', 'description': 'Pack of cute icons for Jack, Ganz, and Ridely.'},
        30: {'title': 'Square Menu Background', 'description': 'The background page-like texture underlay for menus.'},
        31: {'title': 'Menu Texture 1',},
        32: {'title': 'Menu Texture 2',},
        33: {'title': 'Menu Texture 3',},
        34: {'title': 'Menu Texture 4',},
        35: {'title': 'Texture Elements', 'description': 'Icons that get displayed with "texture".'},
        36: {'title': 'Icon Pack 1', 'description': 'Icons for things like markers, arrow, shops.'},
        37: {'title': 'Icon Pack 2', 'description': 'Only two icons: knight and boot.'},
        40: {'title': 'Lootery Menu', 'description': 'Menu elements for the lootery.'},
        41: {'title': 'Training Dummy Icon', 'description': 'Training dummy, checkmark, and unknown dots.'},
        42: {'title': 'Frame Outlines', 'description': 'Frame outlines and some background texture data.'},
        43: {'title': 'Friend Book Background 1', 'description': 'First part of the friend book background.'},
        44: {'title': 'Friend Book Background 2', 'description': 'Second part of the friend book background.'},
        45: {'title': 'Friend Book Background 3', 'description': 'Third part of the friend book background.'},
        46: {'title': 'Friend Book Background Overlay', 'description': 'Overlay Texture for the friend book background.'},
        47: {'title': 'Friend Book Menu Elements', 'description': 'Contains icons, characters and menu elements.'},
        50: {'title': 'Complete Map Menu 1', 'description': 'First part of the map menu in a fully-unlocked state.'},
        51: {'title': 'Complete Map Menu 2', 'description': 'Second part of the map menu in a fully-unlocked state. Also contains Icons.'},
        52: {'title': 'Complete Map Menu 3', 'description': 'Third part of the map menu in a fully-unlocked state. Also contains Icons.'},
        53: {'title': 'Empty Map Menu 1', 'description': 'First part of the map menu in a non-unlocked state.'},
        54: {'title': 'Empty Map Menu 2', 'description': 'Second part of the map menu in a non-unlocked state. Also contains Icons.'},
        55: {'title': 'Empty Map Menu 3', 'description': 'Third part of the map menu in a non-unlocked state. Also contains Icons.'},
        56: {'title': 'Radiata Castle Map 1', 'description': 'Textures for the radiata castle floor layout.'},
        57: {'title': 'Radiata Castle Map 2', 'description': 'Textures for the radiata castle floor layout.'},
        58: {'title': 'Floor Layout Icons', 'description': 'Icons for floor layout menus.'}
    }
    _RANGE00 = 189
    _BANK01 = {
        1: {'title': 'Overlay', 'description': 'Overlay icons, and frame textures.'},
    }
    _RANGE01 = 190
    _BANK02 = [
        "Not Implemented Placeholder", "Not Implemented Attack", "Not Implemented Volty", "Not Implemented Message", "Not Implemented File",
        "Music Disk", "Herb Extract", "Moon Stone", "Cure Needle", "Eye Drops", "Bell Amulet", "Heating Tablet",
        "Mint Drop", "Recovery Pills", "Toadstool Powder", "Book of _", "Strength Berry", "Not Implemented Apple",
        "Defense Berry", "Evasion Berry", "Luck Berry", "Life Berry", "Mystery Berry", "Growth Stone",
        "Not Implemented Bug", "Not Implemented Bread", "Not Implemented Flower", "Not Implemented Flask",
        "Not Implemented Bottle", "Not Implemented Meat", "Not Implemented Fish", 'Not Implemented Bowl',
        "Not Implemented Cutlery", "Not Implemented Silhouette", "Not Implemented Mushroom 1", "Not Implemented Mushroom 2",
        "Not Implemented Mineral", "Not Implemented Cards", "Not Implemented Book", "Not Implemented Scarf",
        "Not Implemented Gem", "Not Implemented Tooth", "Not Implemented Feather", "Not Implemented Stone",
        "Not Implemented Egg", "Not Implemented Crystal", "Not Implemented Bone", "Not Implemented Root",
        "Sage", "Not Implemented Pollen", "Power Bangle", "Warrior Bangle", "Not Implemented Accessory 1",
        "Not Implemented Bangle 2", "Protect Shell", "Monk Bangle", "Skill Upper", "Thief Bangle",
        "Luck Bracelet", "Lucky Charm", "Toughness Bangle", "Life Bangle", "Not Implemented Accessory 3",
        'Not Implemented Accessory 4', "Not Implemented Accessory 5", "Not Implemented Accessory 6",
        "Not Implemented Accessory 7", "Not Implemented Accessory 8", "Not Implemented Accessory 9",
        "Not Implemented Accessory 10", "Not Implemented Accessory 11", "Not Implemented Accessory 12",
        "Not Implemented Accessory 13", "Not Implemented Accessory 14", "Not Implemented Accessory 15",
        "Not Implemented Accessory 16", "Not Implemented Accessory 17", "Not Implemented Accessory 18",
        "Not Implemented Accessory 19", "Not Implemented Accessory 20", "Not Implemented Accessory 21",
        "Not Implemented Accessory 22", "Not Implemented Accessory 23", "Not Implemented Accessory 24",
        "Not Implemented Accessory 25", "Not Implemented Accessory 26", "Not Implemented Accessory 27",
        "Not Implemented Accessory 28", "Not Implemented Accessory 29", "Eagle Crest", "Lion Crest", "Elephant Crest", "Serpent Crest",
        "Not Implemented Accessory 30", "Feather Earring", "Not Implemented Accessory 31", "Not Implemented Accessory 32",
        "Divine Earring", "Hermit's Trophy", "Saint's Trophy", "Pluto's Trophy", "Beckoning Cat", "Not Implemented Accessory 33",
        "Not Implemented Accessory 34", "Not Implemented Accessory 35", "Not Implemented Accessory 36",
        "Power Stone", "Not Implemented Accessory 37", "Not Implemented Accessory 38", "Not Implemented Accessory 39",
        "Not Implemented Accessory 40", "Not Implemented Accessory 41", "Not Implemented Accessory 42",
        "Not Implemented Accessory 43", "Not Implemented Accessory 44", "Training Device", 'VIP Badge',
        'Not Implemented Accessory 45', 'Not Implemented Accessory 46', 'Not Implemented Accessory 47',
        'Leprechaun', 'Magic Mirror', 'Not Implemented Accessory 48', 'Magic Boost', 'Unknown Cross Trinket',
        'Unknown Trophy', "Iron Edge", "Steel Blade", "Knight Edge", "Glory Edge", "Avcoor*", "Jinn",
        "Murasame*", "Kotetsu", "Basilisktos", "Evil Blade", "Hatred Edge", "Phantom Edge", "Spark Edge*",
        "Flame Blade", "Aqua Blade", "Icicle Edge*", "Air Blade", "Breeze Edge*", "Lightning Edge*",
        "Storm Bringer", "Iron Sword", "Steel Saber", "Knight Saber", "Glory Sword", "Holy Sword*",
        "Falvern", "Efreet", "Muramasa", "Bizenosafune", "Rune Saber", "Curse Sword*", "Brain Breaker*",
        "Bind Saber", "Heat Saber", "Flame Sword*", "Lævateinn*", "Blaze Saber", "Grand Saber", "Venom Sword",
        "Cyclone Sword*", "Fake Gram", "Iron Axe", "Steel Axe", "Knight Axe", "Glory Axe", "Ancient Axe",
        "Behemoth", "Death Scythe*", "Hard Chopper*", "Bind Smasher*", "Confuse Axe*", "Fall Smasher",
        "Mist Axe*", "Spark Chopper", "Flame Axe*", "Aqua Chopper", "Icicle Axe", "Mad Axe*", "Rock Axe",
        "Grand Smasher", "Earth Chopper", "Iron Spear", "Steel Pike", "Knight Spear", "Paradigm", "Leviathan",
        "Gungnir*", "Medusa Spear", "Curse Lance", "Brain Shooter*", "Binding Spear", "Duster Pike*",
        "Mad Spear*", "Grand Pike", "Water Pike", "Aqua Spear", "Deep Lance", "Unknown Spear", "Wind Spear*",
        "Brionac","Oratorio", "Requiem", "Sylph Edge",
        "Psycho Edge", "Floating Sword", "Vettea", "Dunvera", "Vaise", "Arabum", "President Blade",
        "Toadstool Blade", "Ganz Sword",
        "Bloody Grip", "Fathmil", "Damascus Blade", "E. Toadstool Sword", "Love Me True", "Blaze Axe", "Heavy Rain", "Bear Smasher", "Toadstool Axe",
        "Titan Pike", "Storm Spear", "Cracked Spear",
        "Toadstool Lance", "Abyss", "Ares Salute", "Adventia", "Entier", "Aldore", "Curozide", "Windmill",
        "Arshaja", "Wellness", "Atmis", "Vipole", "Naruth", "Vatirork", "Asteka",
        "Vathao", "Agroth", "Villhe", "Anviteo", "Suolo", "Wanchu", "Gigantic Hammer", "Flying Foot",
        "Mythril Hammer", "Ore Hammer", "Bloody Hammer", "Iron Hammer", "Aron", "Esthia", "Raven Claw",
        "Answerer", "Steel Dagger", "Butterfly Knife", "Iron Knife", "Kogitsunemaru", "Heat Dagger",
        "Morningstar", "Head Basher", "Earth Crusher", "Bronze Crusher", "Symphonia", "Whip", "Predator Claw",
        "Shovel Claw", "Chupa Claw", "Farmer's Hoe", "Spade", "Crossbow", "Truncheon", "Halberd",
        "Oak Club", "Ladle", "Spatula", "Justice Ruling", "Winner Ruling", "Tobacco Pipe", "Bottle",
        "Guiron Tree", "Walking Stick", "Metal Pipe", "Fly Swatter", "Toadstool Bazooka",
        "Slingshot", "Tamtam Slingshot", "Frying Pan", "Bokken", "Zengen", 'Ancient Magic Book', 'Iron Gauntlet',
        'Vagabond\'s Guitar', 'Knight Axe', 'Handmade Tunic', "Leather Armor", "Sharkskin", "Iron Breastplate",
        "Wind Garb", "Wooden Breastplate", "Iron Plate", "Scale Armor", "Divine Coat", "Plate Armor",
        "Dragon Scale", "Demon Mail", "Ore Armor", "Alfestrain", "Samurai Armor", "Absolute Guard",
        "Valiant Mail", "Fayt Armor", "Robot Suit", "Recruitment Suit", "Glory Armor", "Ganz's Armor",
        "Ridley's Clothes", "Trainee's Wear", "Valiant Mail", "Steel Guard",
        "Leather Tunic", "Legendary Armor", "Metal Body", "Enchanted Robe", "Bushin Armor", "Red Lion Armor",
        "Ancient Mail", "Resist Coat", "Samurai Armor", "Wing Garb", "Crocogator Skin", "Plate Armor",
        "Plate Armor", "Plate Armor", "Axe Head", "Plate Armor", "Plate Armor", "Plate Armor", "Crocogator Skin",
        "Crocogator Skin", "Resist Coat", "Plate Armor", "Plate Armor", "Crocogator Skin", "Normal Clothes",
        "Mage Armor", "Great Mage Robe", "Magical Dress", "Mage's Robe", "Witch Cloak", "Vareth Uniform",
        "High Priest's Gown", "Dual Cloak", "Peacock Garb", "Dry Cloak", "Master's Garment", "Robe of Order",
        "Monk's Robe", "Robe of Order", "Nun's Robe",
        "Monster Cloak", "Scouts Suit", "Hades Robe", "Black Dress", "Leather Clothes", "Disguise",
        "Hoodlum's Clothes", "Assassin Suit", "Scouts Suit", "Chrome Clothes", "Chrome Clothes", "Scouts Suit",
        "Not Implemented Armor 1", "Chrome Clothes", "Chrome Clothes", "Knight Armor", "Knight Armor", "Normal Clothes",
        "Not Implemented Armor 2", "Sacred Blue Gown",
        "Children's Clothes", "Herdsman's Clothes", "Children's Clothes",
        "Farming Clothes", "Farming Clothes", "Cook's Apron","Farming Clothes", "Linen Cuirass", "Cloth Apron",
        "Green Robe", "Grass Clothes",
        "Grass Clothes", "Grass Clothes", "Grass Clothes", "Autumn Leaf Cloak", "Leaf Clothes", "Leaf Clothes", "Leaf Clothes",
        "Goblin Suit",
        "Goblin Suit", "Goblin Suit", "Goblin Suit", "Goblin Suit", "Goblin Suit", "Goblin Suit", "Goblin Suit",
        "Goblin Suit", "Goblin Suit", "Goblin Suit", "Goblin Suit", "Goblin Suit", "Big Toadstool Suit",
        "Toadstool Suit", "Toadstool Suit", "Toadstool Suit",
        "Shoulder Pads", "Vareth Uniform", "Shabby Mail", "Shabby Mail", "Glory Armor",
        "Normal Clothes", "Nurse Uniform", "Smelly Old Clothes",
        "Children's Clothes", "Valiant Mail", "Not Implemented Armor 3", "Glory Armor", "Trainee's Wear",
        "Umbrella", "Herb Extract S", "Herb Extract DX", "Herb Extract MAX", "Revival Stone", "Cleansing Stone",
        "Moon Stone Chip", "Revival Stone Chip", "Cure Drop", "Cooling Spray", "Holy Water", "Flexibility Lotion",
        "Invincibility Med", "Mud Powder", "Mustard Powder", "Startle Powder", "Snow Powder", "Magma Powder",
        "Panic Powder", "Mass of Enmity", "Cement Powder", "Tsuchinoko Dumpling", "Flee Ball", "Analysis Ball",
        "Celestial Nectar", "Holy Sword Gram", "Seraphic Garb", "Evening Bloom", "David's Letter",
        "Carlos's Contact Lens", "Faraus's Med/Voynich Book", "Key to Repository", "Man's Picture", "Church Bulletin", "Worn Belt",
        "Lulu's Cat", "Matango Larva", "Gobpakken Seed", "Pointura's Thread", "Blood Orc's Horn" "Collection Bag",
        "Bridge Blueprints", "Piglet", "King's Toadstool", "Polpo's Soup", "Tria Milk", "Bligh's Pipe",
        "Deathclover Larva", "Nightstone", "Blue Orb", "Green Orb", "Red Orb", "Purple Orb", "Orb",
        "Smilodon's Fang", "Crocogator's Skin", "Arbitrator", "Boundary Crest", "Recruitment Flyer",
        "Bundle of Dagol", "Funny Money", "Really Funny Money", "Written Request", "Royal Knight Charter",
        "Dwarf Liquor", "Elven Wine", "Parsec's Match", "Shiny Ore", "Dwarf's Parcel", "Grass Clothes",
        "Grass Clothes", "Leaf Clothes", "Leaf Clothes", "Leaf Clothes", "Magical Dress"
    ]
    _RANGE02 = 191
    _BANK03 = {
        1000: {'title': 'Friends Book Extra 1', 'description': 'First part of the "extra" friend book entry.'},
        1001: {'title': 'Friends Book Extra 1', 'description': 'Second part of the "extra" friend book entry.'},
        1002: {'title': 'Friends Book Extra 1', 'description': 'Third part of the "extra" friend book entry.'},
        1003: {'title': 'Friends Book In-Progress', 'description': 'Friend book in-progress texture.'},
        1004: {'title': 'Friends Book Complete', 'description': 'Friend book complete texture.'},
    }
    _RANGE03 = 192
    _BANK06 = {
        1: {'title': 'Icons', 'description': 'Pack of icons'}
    }
    _RANGE06 = 195
    _BANK10 = {
        1: {'title': 'Poster 1', 'description': 'First part of a post texture.'},
        2: {'title': 'Poster 2', 'description': 'Second part of a post texture.'},
        3: {'title': 'Poster 3', 'description': 'Third part of a post texture.'},
        4: {'title': '1000 Bill 1', 'description': 'First part of the 1000 dollar bill texture.'},
        5: {'title': '1000 Bill 2', 'description': 'Second part of the 1000 dollar bill texture.'},
        6: {'title': '1000 Bill 3', 'description': 'Third part of the 1000 dollar bill texture.'},
        7: {'title': '5000 Bill 1', 'description': 'First part of the 5000 dollar bill texture.'},
        8: {'title': '5000 Bill 2', 'description': 'Second part of the 5000 dollar bill texture.'},
        9: {'title': '5000 Bill 3', 'description': 'Third part of the 5000 dollar bill texture.'},
        10: {'title': 'Legend 1 1', 'description': 'First part of the first legend texture.'},
        11: {'title': 'Legend 1 2', 'description': 'Second part of the first legend texture.'},
        12: {'title': 'Legend 1 3', 'description': 'Third part of the first legend texture.'},
        13: {'title': 'Legend 2 1', 'description': 'First part of the second legend texture.'},
        14: {'title': 'Legend 2 2', 'description': 'Second part of the second legend texture.'},
        15: {'title': 'Legend 2 3', 'description': 'Third part of the second legend texture.'},
        16: {'title': 'Legend 3 1', 'description': 'First part of the third legend texture.'},
        17: {'title': 'Legend 3 2', 'description': 'Second part of the third legend texture.'},
        18: {'title': 'Legend 3 3', 'description': 'Third part of the third legend texture.'},
        19: {'title': 'Flashback Dwarf Meeting 1', 'description': 'First part of the Dwarf Meeting flashback texture'},
        20: {'title': 'Flashback Dwarf Meeting 2', 'description': 'Second part of the Dwarf Meeting flashback texture'},
        21: {'title': 'Flashback Dwarf Meeting 3', 'description': 'Third part of the Dwarf Meeting flashback texture'},
        22: {'title': 'Flashback Ridley City of Flowers 1', 'description': 'First part of the Ridley City of Flowers flashback texture'},
        23: {'title': 'Flashback Ridley City of Flowers 2', 'description': 'Second part of the Ridley City of Flowers flashback texture'},
        24: {'title': 'Flashback Ridley City of Flowers 3', 'description': 'Third part of the Ridley City of Flowers flashback texture'},
        25: {'title': 'Flashback Cairn and Gawain 1', 'description': 'First part of the Cairn and Gawain flashback texture'},
        26: {'title': 'Flashback Cairn and Gawain 2', 'description': 'Second part of the Cairn and Gawain flashback texture'},
        27: {'title': 'Flashback Cairn and Gawain 3', 'description': 'Third part of the Cairn and Gawain flashback texture'},
        28: {'title': 'Flashback Hydra fight 1 1', 'description': 'First part of the Hydra fight 1 flashback texture'},
        29: {'title': 'Flashback Hydra fight 1 2', 'description': 'Second part of the Hydra fight 1 flashback texture'},
        30: {'title': 'Flashback Hydra fight 1 3', 'description': 'Third part of the Hydra fight 1 flashback texture'},
        31: {'title': 'Flashback Hydra fight 2 1', 'description': 'First part of the Hydra fight 2 flashback texture'},
        32: {'title': 'Flashback Hydra fight 2 2', 'description': 'Second part of the Hydra fight 2 flashback texture'},
        33: {'title': 'Flashback Hydra fight 2 3', 'description': 'Third part of the Hydra fight 2 flashback texture'},
        34: {'title': 'Flashback Hydra fight 3 1', 'description': 'First part of the Hydra fight 3flashback texture'},
        35: {'title': 'Flashback Hydra fight 32', 'description': 'Second part of the Hydra fight 3flashback texture'},
        36: {'title': 'Flashback Hydra fight 33', 'description': 'Third part of the Hydra fight 3flashback texture'},
        37: {'title': 'Flashback Cairn Diseased 1', 'description': 'First part of the Cairn Diseased flashback texture'},
        38: {'title': 'Flashback Cairn Diseased 2', 'description': 'Second part of the Cairn Diseased flashback texture'},
        39: {'title': 'Flashback Cairn Diseased 3', 'description': 'Third part of the Cairn Diseased flashback texture'},
        40: {'title': 'Fancy Flashback Hydra fight 3 1', 'description': 'First part of the fancy Hydra fight 3 flashback texture'},
        41: {'title': 'Fancy Flashback Hydra fight 3 2', 'description': 'Second part of the fancy Hydra fight 3 flashback texture'},
        42: {'title': 'Fancy Flashback Hydra fight 3 3', 'description': 'Third part of the fancy Hydra fight 3 flashback texture'},
    }
    _RANGE10 = 199

    @classmethod
    def iter_entries(cls) -> Iterator[tuple[str, dict[str, Any]]]:
        dict_banks = [
            (cls._RANGE00, cls._BANK00),
            (cls._RANGE01, cls._BANK01),
            (cls._RANGE03, cls._BANK03),
            (cls._RANGE06, cls._BANK06),
        ]
        # Metadata for Texture Bank 00, 01, 03, 06
        for disk_idx, bank in dict_banks:
            for child_idx, data in bank.items():
                name = data.get('title', 'Unknown')
                desc = data.get('description', '')
                # Base (Compressed) entry
                base_meta: dict[str, Any] = {
                    'title': f'{name} (Compressed)',
                    'tags': ('Texture',)
                }
                if desc:
                    base_meta['description'] = desc
                yield f'{disk_idx}.{child_idx}', base_meta

                # FIS entry
                fis_meta: dict[str, Any] = {
                    'title': name,
                    'tags': ('Texture', 'FIS')
                }
                if desc:
                    fis_meta['description'] = desc
                yield f'{disk_idx}.{child_idx}.0', fis_meta

        # Metadata for Texture Bank 02
        disk_idx = cls._RANGE02
        for child_idx, name in enumerate(cls._BANK02):
            yield f'{disk_idx}.{child_idx + 1}', {
                'title': f'{name} Icon (Compressed)',
                'tags': ('Texture',)
            }
            yield f'{disk_idx}.{child_idx + 1}.0', {
                'title': f'{name} Icon',
                'tags': ('Texture', 'FIS')
            }
        # Metadata for Texture Bank 10
        (cls._RANGE10, cls._BANK10)
        disk_idx = cls._RANGE10
        for child_idx, data in cls._BANK10.items():
            yield f'{disk_idx}.{child_idx}', {
                'title': data.get('title'),
                'description': data.get('description'),
                'tags': ('Texture', 'FIS')}

_STATIC_METADATA_SOURCES: list[StaticMetadataSource] = [
    PhysicalFileCategories, PhysicalFileNames, EntityPackSections, MapSections,
    IOPModules, EventScripts, CharaPortraits, TextureBanks
]

###---------------------------------------- Packages ---------------------------------------------###

_MAX_PACK_SLOTS = 10  # entity pack slots for the main header

###---------------------------------------- Layout ---------------------------------------------###

_TOC_REGION    = 'toc'
_FIXED_TOC_LBA = 494979
_SENTINEL_LBAS = frozenset({-1, 0xFFFFFFFF})  # decode() reads unsigned, so a stored -1 comes back as 0xFFFFFFFF

class RadiBuilder(BaseSourceBuilder):
    '''Disk layout for Radiata Stories, in disk order:
    system areas + root directory, runtime ISO files, TOC padding, TOC, VFS payload.
    tree reminder: self.source_root is the PHYSICAL tree (executable, SYSTEM, ... plus the boundary node),
    self.vfs_root is the boundary node whose children are the TOC derived virtual files.'''

    def build_layout(self) -> None:
        self._emit_prologue()
        self._emit_runtime_files()
        self._emit_toc_placement()
        self._emit_toc_table()
        self._emit_vfs_payload()

    def _emit_prologue(self) -> None:
        '''System area, PVD record, area up to the root directory, then the root directory itself.'''
        geo = self.geometry
        sys_area_1_len = (geo.iso_9660_pvd * geo.sector_size) + geo.pvd_byte_offset
        self.add_region(RawCopyRegion(0, sys_area_1_len, 'System Area', self.handle))
        self.add_region(RawCopyRegion(sys_area_1_len, self.pvd.entry_length, 'PVD Record', self.handle))
        pvd_end = sys_area_1_len + self.pvd.entry_length
        self.add_region(RawCopyRegion(pvd_end, self.pvd.lba - pvd_end, 'System Area 2', self.handle))
        self.add_region(RootDirectoryRegion(
            fixed_size=self.pvd.file_size,
            sector_size=geo.sector_size,
            original_bytes=self.handle.pread(self.pvd.lba, self.pvd.file_size),
            keep_unmatched=False,
        ), name='root_dir')

    def _emit_runtime_files(self) -> None:
        '''Runtime ISO files (IOPRP300, SYSTEM, executable) and the gaps between them, in disk order,
        each registered in the root directory. The executable must receive the TOC offset patch.'''
        root_dir = self.named.get('root_dir')
        if not isinstance(root_dir, RootDirectoryRegion):
            raise ValueError('root directory region missing, _emit_prologue must run first')
        nodes = [child for child in self.source_root.children if child.is_physical or not child.is_boundary]
        nodes += self.gap_nodes
        nodes.sort(key=lambda node: node.offset)
        suffixes = tuple(self.source.runtime_file_names)
        patched = False
        exec_names: list[str] = []
        for node in nodes:
            if not node.name.endswith(suffixes):
                continue
            if node.name.startswith('AreaBefore'):
                self.add_region(RawCopyRegion(node.offset, node.size, node.name, self.handle))
                continue
            is_exec = node.name.startswith(('SLUS', 'SLPM'))
            sites = self.sites_for(node) or self._locate_sites(node)
            if sites:
                region = BinaryPatchRegion(
                    node, self.handle, self.geometry.sector_size,
                    patch_targets=sites, rebuild_context=self.rebuild_context,
                )
                patched = True
            elif node in self.staged and node.pending_data is not None:
                region = StagedDataRegion(node, self.geometry.sector_size)
            else:
                region = RawCopyRegion(node.offset, node.size, node.name, self.handle)
            if is_exec:
                exec_names.append(f'{node.name} (sites={sites!r})')
            self.add_region(region)
            root_dir.front_section_entries.append((node, region))
        if not patched:
            raise ValueError(
                'The executable received no TOC offset patch. '
                f'Executable nodes seen: {exec_names or "none"}; '
                f'patch sites supplied to the builder: {[(s.node.name, type(s.patch).__name__) for s in self.patch_sites]}; '
                f'active patches: {[type(p).__name__ for p in self.source.patches_for(self.flags)]}; '
                f'slimmed={self.has_flag("SLIMMED")}'
            )

    def _locate_sites(self, node) -> list[PatchSite]:
        '''Fallback: resolve patch sites for this node from the builder's own node objects when the
        sites supplied at construction do not include it (e.g. they were located against other node
        instances or a different node list). Uses the same PatchLocator as the normal path.'''
        if not self.source.patches_for(self.flags):
            return []
        sites = PatchLocator().locate_all(self.handle, [node], self.source.patches_for(self.flags))
        if sites:
            self.log(f'Patch sites for {node.name} were not supplied to the builder; located them directly')
        return sites

    def _emit_toc_placement(self) -> None:
        '''Padding between the ISO filesystem and the TOC. Normally pads to the TOC LBA the executable
        expects. With SLIMMED it only sector-aligns so the TOC follows immediately, and the executable
        patch carries the new LBA.'''
        sector = self.geometry.sector_size
        if self.has_flag('SLIMMED'):
            self.add_region(AlignRegion(sector, 'TOC_alignment_padding'), name='toc_padding')
        else:
            self.add_region(PadToLbaRegion(_FIXED_TOC_LBA, sector, 'TOC_offset_Padding'), name='toc_padding')

    def _emit_toc_table(self) -> None:
        '''The scrambled TOC. Entries are filled by _emit_vfs_payload, so this must come before it.'''
        self.add_region(TocRegion(
            total_entries=self.source.toc_total_entries,  # changing this means the executable must be patched too
            sector_size=self.geometry.sector_size,
            scramble_fn=self.source.scramble_toc,
        ), name=_TOC_REGION)

    def _emit_vfs_payload(self) -> None:
        '''VFS data, one sector aligned region per node, recorded in the TOC. TOC slot 0 describes the
        TOC itself (no region); sentinels are zero size entries.'''
        toc_region = self.named.get(_TOC_REGION)
        if not isinstance(toc_region, TocRegion):
            raise ValueError('TOC region missing, _emit_toc_table must run first')
        sector = self.geometry.sector_size
        for idx, child in enumerate(self.vfs_root.children):
            if idx == 0:  # self-reference
                toc_region.entries.append((child, None))
                continue
            orig_lba = self.toc[idx].lba if idx < len(self.toc) else 0
            if (child.size == -1 and child not in self.staged) or orig_lba in _SENTINEL_LBAS:
                toc_region.entries.append((child, SentinelRegion(child)))
                continue
            if child in self.staged and child.pending_data is not None:
                region = StagedDataRegion(child, sector)
            else:
                region = RawCopyRegion(child.offset, child.size, child.name, self.handle)
            toc_region.entries.append((child, region))
            self.add_region(region, alignment=sector)
###---------------------------------------- Patch and Flags ---------------------------------------------###

class RadiRebuildFlags(SourceRebuildFlags):
    '''
    Which patches need to be applied to the filesystems before or during the ISO rebuild.
    Patches should be designed as independent and combinable.
    Adding a new patch should be as simple as adding a new flag and logic(Navigation + Overwrite).

    NONE              - No patches are applied, in this case that means the executable is patched to the default offset
    SLIMMED           - SourceHandler, patch out non-essential runtime data to save 1GB
    CUTSCENE_SKIPPER  - EvdHandler, scan and patch all story events with the appropriate near instant termination
    '''
    NONE              = 0
    SLIMMED           = auto()
    CUTSCENE_SKIPPER  = auto()

class TocOffsetPatch(BasePhysicalPatch):
    '''Points the executable to the correct TOC offset.'''
    name    = 'default build'
    flag    = RadiRebuildFlags.NONE

    def candidate_selector(self, all_nodes: list[VfsNode]) -> list[VfsNode]:
        return [n for n in all_nodes if not n.is_boundary and (n.name or '').startswith(('SLUS', 'SLPM'))]

    def locate(self, raw_data: bytes) -> int | None:
        return _find_masked_immediate(raw_data)

    def compute_value(self, context: RebuildContext) -> int:
        return context.lba_of(_TOC_REGION)

    def apply(self, data: bytearray, offset: int, value: int) -> None:
        _write_masked_immediate_value(data, offset, value)

class CutsceneSkipperPatch(BaseVirtualPatch):
    '''Skips cutscene loading by modifying the cutscene skipper function.'''
    name        = 'Cutscene Skipper'
    flag        = RadiRebuildFlags.CUTSCENE_SKIPPER
    description = 'Skips cutscenes by terminating them near-instantly.'
    action      = 'Skip cutscenes'
    hid         = (186,)

class SlimmedPatch(BasePhysicalPatch):
    '''Marker patch for the SLIMMED option.'''
    name        = 'Slimmed rebuild'
    flag        = RadiRebuildFlags.SLIMMED
    description = 'Saves 1GB of space by removing non-essential runtime data.'

    def candidate_selector(self, all_nodes: list[VfsNode]) -> list[VfsNode]:
        return []

    def locate(self, raw_data: bytes) -> int | None:
        return None

    def compute_value(self, context: RebuildContext) -> int:
        return 0

    def apply(self, data: bytearray, offset: int, value: int) -> None:
        pass

###---------------------------------------- Registrations ---------------------------------------------###

_KNOWN_BUILDS: dict[str, str] = {
    '7ee1ab6550739833f757ccc9db23cc36': 'Prototype',
    'afb46b880ee88e93b1f2ccb417e02977': 'USA release',
    'f5fbce42d0d943c01e506c7f7d7e24e2': 'JPN release',
}

@Registry.register_source
class RadiataStoriesSource(BaseSource):
    '''Radiata Stories (PS2): ISO9660'''
    display_name        = 'Radiata Stories'
    metadata_path       = 'ui/assets/radi_metadata.json'
    build_hashes        = _KNOWN_BUILDS
    geometry            = _GEOMETRY
    toc_total_entries   = 0x1200
    hidden_toc_indices  = frozenset({0, 5})
    runtime_file_names  = frozenset(_RUNTIME_REQUIRED_FILES | _RUNTIME_EXECUTABLE_CANDIDATES)
    rebuild_flags       = RadiRebuildFlags
    patches             = (TocOffsetPatch(), CutsceneSkipperPatch(), SlimmedPatch())
    builder             = RadiBuilder

    _SIGNATURE          = b'RADIATA'
    _SIGNATURE_OFFSET   = 0x28
    _TOC_SEED           = 0x13578642

    ### Identification
    def matches(self, handle: BlockDevice) -> bool:
        offset = (self.geometry.iso_9660_pvd * self.geometry.sector_size) + self._SIGNATURE_OFFSET
        return handle.pread(offset, len(self._SIGNATURE)) == self._SIGNATURE

    def validate_filesystem(self, root_children: list[VfsNode]) -> None:
        found_names = {child.name for child in root_children}
        has_system  = _RUNTIME_REQUIRED_FILES.issubset(found_names)
        has_executable = bool(_RUNTIME_EXECUTABLE_CANDIDATES.intersection(found_names))
        if not (has_system and has_executable):
            raise ValueError(f'Invalid filesystem: missing {_RUNTIME_REQUIRED_FILES - found_names}')

    ### TOC
    def locate_toc(self, root: VfsNode, handle: BlockDevice) -> int:
        self.resolve_boundary(root)
        boot = next((node for node in root.children if node.name in _RUNTIME_EXECUTABLE_CANDIDATES), None)
        if not boot:
            raise ValueError('No boot executable found, cannot open source.')
        boot_bytes = handle.pread(boot.offset, boot.size)
        pos = _find_masked_immediate(boot_bytes)
        if pos is None:
            raise ValueError('TOC could not be found, cannot open source.')
        return _read_masked_immediate_value(boot_bytes, pos)

    def scramble_toc(self, flat_toc: list[int]) -> list[int]:
        '''Symmetric XOR for 3 column toc'''
        total = self.toc_total_entries
        key = self._TOC_SEED
        scramble = flat_toc[:]
        for i in range(total):
            scramble[0 * total + i] ^= key
            key ^= (key << 1) & 0xFFFFFFFF
            scramble[1 * total + i] ^= key
            key ^= (~self._TOC_SEED) & 0xFFFFFFFF
            scramble[2 * total + i] ^= key
            key ^= ((key << 2) ^ self._TOC_SEED) & 0xFFFFFFFF
        return scramble

    def decode_toc(self, handle: BlockDevice, toc_lba: int) -> list[TocEntry]:
        total = self.toc_total_entries
        offset = toc_lba * self.geometry.sector_size
        raw = handle.pread_view(offset, total * 3 * 4)
        flat = self.scramble_toc(list(struct.unpack(f'<{total * 3}I', raw)))
        entries = []
        for i in range(total):
            lba = flat[i]
            entries.append(TocEntry(
                id=i,
                lba=lba,
                size=flat[total + i],
                offset=lba * self.geometry.logical_sector_size,
                logical_id=flat[(total * 2) + i],
                name=f'File {i:04d}'
            ))
        return entries

    def encode_toc(self, entries: list[TocEntry]) -> bytes:
        total = self.toc_total_entries
        flat = [0] * (total * 3)
        for i, entry in enumerate(entries):
            flat[i] = entry.lba
            flat[total + i] = entry.size
            flat[(total * 2) + i] = entry.logical_id
        return struct.pack(f'<{total * 3}I', *self.scramble_toc(flat))

    ### Extensions
    def resolve_extension(self, node: VfsNode, header: bytes) -> str:
        node.extension = lookup_extension(header)
        if node.extension == '.bin':
            PK3_MAGIC = 0x004E000
            check_1, check_2 = unpack_from('<II', header, 0x10)
            if not check_1 or not check_2:
                node.extension = '.bin'
            elif check_2 % PK3_MAGIC == 0 and check_1 % PK3_MAGIC == 0:  # header is pk3 divisible
                node.extension = '.pk3'
            else:
                node.extension = '.bin'
        return node.extension

    ### Packages / conflicts / metadata
    def members_for(self, node: VfsNode, intent: PackageIntent, links: LinkLookup) -> tuple[PackageMember, ...]:
        '''A "Kods" container needs its datacenter header (and, when importing, the entity pack sub headers).'''
        hid    = node.hierarchical_id
        header = links.link_of(hid)
        if header is None:
            return ()
        members = [PackageMember(HEADER_ROLE, header)]
        if intent is PackageIntent.IMPORT:
            for slot in range(_MAX_PACK_SLOTS):
                sub_header = links.link_of(hid + (slot,))
                if sub_header is not None:
                    members.append(PackageMember(slot_header_role(slot), sub_header, required=False))
        return tuple(members)

    def find_conflicts(self, incoming: VfsNode, pending: frozenset[VfsNode], links: LinkLookup) -> list[ConflictFinding]:
        hid  = incoming.hierarchical_id
        header_hid = links.link_of(hid)
        findings: list[ConflictFinding] = []
        for other in pending:
            other_hid = other.hierarchical_id
            if header_hid is not None and other_hid == header_hid:
                findings.append(ConflictFinding(other, f'{incoming} depends on header from {other} which has pending modifications'))
            elif links.link_of(other_hid) == hid:
                findings.append(ConflictFinding(other, f'{other} depends on header from {incoming} which has pending modifications'))
        return findings

    def build_metadata(self, store: NodeMetadataStore) -> int:
        count = store.ingest_static_sources(_STATIC_METADATA_SOURCES)
        count += store.register_many(
            (hid_str, {'target': header_hid, 'extension': '.kods'})
            for hid_str, header_hid in DatacenterTargets.to_hid_str_map()
        )
        return count
