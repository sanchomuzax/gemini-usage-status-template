# Quota-aware orchestration prompt (Antigravity / Google AI Pro)

Paste the block below into an orchestrator agent's instructions (or a project
`AGENTS.md`) to make it budget its own work on the Antigravity CLI (`agy`).

It depends on `budget_check.py` from this repo, which returns a verdict and an
exit code (`0` GO, `1` CAUTION, `2` STOP, `3` UNKNOWN).

---

## Kvótatudatos munkavégzés (Antigravity)

Ez a gép figyeli a Google AI Pro előfizetés Antigravity-keretét. Mielőtt nagy
feladatba kezdesz az `agy`-val, kérdezd meg a keret állását, és igazodj hozzá.
A parancs nem kerül kvótába, és pár másodperc alatt lefut:

```sh
python3 ~/Documents/gemini-usage-status/budget_check.py --brief
```

Példa válasz:
`GO | live | acct you | gemini | session 12.0% | weekly 31.0% | burn 0.9x`

### Melyik keretet nézd

Az Antigravity három külön keretet számol, mindegyiknek saját 5 órás és heti
ablaka van. Azt kérdezd, amelyiken dolgozni fogsz:

| kapcsoló | keret | modellek |
|---|---|---|
| `--group gemini` (alapértelmezés) | Gemini Models | Gemini Flash, Gemini Pro |
| `--group 3p` | Claude and GPT models | Claude Opus/Sonnet, GPT-OSS az Antigravityben |
| `--group argon` | Gemini 4 Argon | külön heti keret, 5 órás ablak nélkül |

Ha `--model claude-...` vagy más harmadik féltől származó modellel futtatod az
`agy`-t, a `3p` keretet kérdezd. Ha Argonnal, akkor az `argon`-t.

### Mindig friss adatból dolgozz

A válasz mindig kiírja, honnan jön a szám:

- `live`: élő lekérdezés a szervertől. Ez a jó eset.
- `CACHED ... m old`: az élő lekérdezés nem sikerült (más gépen futsz, vagy az
  `agy` bejelentkezése lejárt), ezért a repóba commitolt pillanatképet olvassa.
  Frissíts, és csak utána dönts:

```sh
git -C ~/Documents/gemini-usage-status pull --quiet
python3 ~/Documents/gemini-usage-status/budget_check.py --brief
```

Ha a pillanatkép 90 percnél régebbi, a verdikt `UNKNOWN`. Ha a pillanatkép 5 órás
ablaka azóta lejárt, a parancs nem mondja meg a régi számot, hanem
`session unknown (stale)` jelzést ad, és ilyenkor GO-t sem enged.

### Mikor ellenőrizd

- Minden munkamenet elején, mielőtt tervet készítesz.
- Minden subagent-indítás előtt, különösen ha többet indítanál párhuzamosan.
- Fázisok között.
- Ha egy subagent hibával tér vissza (lásd lent).

### Mit jelentenek a verdiktek

**GO**: nincs korlátozás.

**CAUTION**: van keret, de fogy. Ne indíts párhuzamos fan-outot, szűkítsd a
hatókört, a mechanikus munkára válassz olcsóbb modellt, és halaszd a ráérős
dolgokat a keret nullázódása utánra. Ami már fut, azt fejezd be.

**STOP**: ne kezdj új munkát. Hozd a félkész változtatást konzisztens állapotba,
commitold egy WIP branchre, írj rövid átadó összefoglalót, mondd meg a
felhasználónak, mikor nullázódik a keret (a JSON-kimenet `*_resets_in_minutes`
mezői), és állj meg.

**UNKNOWN**: a kvóta nem olvasható. Próbáld a fenti `git pull`-t; ha utána is
UNKNOWN, dolgozz CAUTION szabályok szerint, és szólj a felhasználónak, hogy a
keretfigyelés épp nem lát. Az `agy` `/usage` parancsa pontos számot ad.

### Ha egy subagent hibával tér vissza

A kvótahiba nem kódhiba. Ha egy futás elszáll, mielőtt bármit javítanál, futtasd
le a `budget_check.py`-t. Ha a verdikt STOP, vagy a hibaüzenetben `quota`,
`limit`, `429` vagy `RESOURCE_EXHAUSTED` szerepel, ne írd át a kódot, ne kezdj
újrapróbálkozási ciklusba, hanem checkpointolj a STOP eljárás szerint, és szólj.

### Profil-címke a kimenetben

A `[conserve]` vagy `[greedy]` címke a felhasználó által beállított költési
politika, nem az, hogy a fiók kimerülőben van. A verdiktet mindig kövesd.

Ne kérdezz rá minden ellenőrzés eredményére; csak akkor jelezz, ha a verdikt
CAUTION-re vagy STOP-ra vált, vagy ha emiatt megváltoztatod a tervet.
