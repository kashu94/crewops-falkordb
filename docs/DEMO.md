# Demo video script (about 3 minutes)

Before recording: `docker compose up -d falkordb`, `uv run crewops load --replace`,
`uv run crewops serve`. Open FalkorDB Browser (http://localhost:3000) in a second tab.

1. **Intro (15 s).** A captain calls in sick. Finding cover depends on crew, trips, rules,
   reserves and desks, which are all connected.
2. **Report (20 s).** Sign in as `imran.sheikh` and report C-1042 unavailable 13-14 Oct.
   The triage agent picks it up within a couple of seconds and assigns it to desk-1.
3. **Analysis (30 s).** Open D-0001: 6 flights, 841 passengers, 9 captains checked, 4 legal,
   options ranked by cost. Show the graph panel.
4. **Assistant (40 s).** As `ananya.iyer`: "Who covers C-1042 on 13 Oct?", then
   "Is she legal for it?", then "Why not C-2087 for D-0001?". Open the Cypher queries under
   an answer.
5. **Approval (30 s).** Submit as Ananya, approve as `meera.bhatt`. Show the trail. In the
   Browser run:
   `MATCH (c:Crew)-[a:ASSIGNED]->(:Pairing {id:'P-2291'}) RETURN c.id, a.role, a.via_plan`
6. **Memory and agents (30 s).** Sign in as Ananya again and say "Hello" (she gets her last
   question back). Run `uv run crewops triage --workers 4 --once` after reporting a few
   disruptions. Show the memory graphs in the Browser.
7. **Algorithms (15 s).** "Which station is most critical?" and "Best route from GOI to CCU?".
8. **Wrap-up (10 s).** FalkorDB is the only data store.
