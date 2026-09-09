# Grok critique: Redslip

Adversarial review held over the `grokweb` CLI on the x.com transport, conversation id
`redslip-kill`, model grok-4, four turns. Turn 1 used reasoning mode. The transcript is
verbatim: the prompt sent, then Grok's reply, for each turn.

Grok has live retrieval, so its claims about what competing ClickHouse-track entries are
shipping and about ADK API surfaces are leads, not ground truth. Every ADK class, import
path and method named below was checked against the installed `google-adk` 2.8.0 before
any of it was written into the project. Where Grok was wrong, the verification note says
so.


---

## Turn 1: prompt sent

```
You are an adversarial hackathon judge. Be brutal, specific, and concrete. No hedging, no "consider adding". I want reasons this LOSES.

CONTEXT: Agentic Cinema: The Blockbuster Hackathon (Google + Devpost, agentic-cinema.devpost.com). Submission closes in ~12 hours. 9,895 registrations. Five identical prize buckets, one per partner track (Parallel, ClickHouse, Grafana, IBM, Replit). I compete only inside the ClickHouse track. 3 winning slots on that track.

THE BRIEF, VERBATIM:
"Build a functional agent powered by Gemini and Google Cloud Agent Builder that integrates a Partner Entity's product or MCP to power a real media & entertainment workflow."
"solve critical bottlenecks across the entertainment and media value chain, specifically targeting the workflows of filmmakers, screenwriters, studio crews, or fans."
Verbs from the brief: orchestrate, solve enterprise friction, "show off a deterministic, multi-step agent". Not chat.

JUDGING CRITERIA (verbatim):
1. Technological Implementation: how well built, and how effectively it uses Google Cloud AND the Partner services.
2. Design: a complete, coherent product experience, not a technical proof of concept.
3. Potential Impact: credible, specific case for a real problem for a real audience, and does the solution actually address it BASED ON WHAT IS DEMONSTRATED.
4. Quality of the Idea: creative, NON-OBVIOUS use of Google Cloud + Partner, and genuine understanding of the problem space.

HARD GATES: hosted judge-testable URL; 3-minute demo video of a FUNCTIONING PRODUCT (explicitly not a cinematic trailer); public repo with all source and actual RUNTIME use of Google Cloud + the partner service, imported and called in code, not just named in the README; OSS license detectable in the GitHub About sidebar.

CLICKHOUSE TRACK HARD RUNTIME REQUIREMENT, VERBATIM: "Must actively use ClickHouse at runtime via the official mcp-clickhouse MCP server, against ClickHouse Cloud or self-hosted."

GOOGLE'S EXPLICIT STEER: "build agents natively using the Agent Development Kit (ADK) instead of external wrapper libraries."

JUDGE COMPOSITION: 23 judges. On the ClickHouse track: ClickHouse Director of Engineering AI/ML, plus a ClickHouse full-stack AI/ML engineer. They built mcp-clickhouse. Plus ~13 Google judges, mostly engineers and PMs (Cloud SQL, Play Ads, Search Intelligence, AI Integrity, GDC networking, Forward Deployed GenAI). These are working engineers on the product, not media people. They will spot a decorative integration instantly.

MY ENTRY: "Redslip", catalog-scale archive delivery triage.
The problem: a distributor uploads a finished film to a streamer, eleven days later it comes back rejected because integrated loudness was out of spec, or subtitle cues broke the reading-speed limit, or a black segment flagged as damage. Nobody measured the numbers before delivery. Redslip prevents this at catalog scale: point it at a film archive and it tells you, with machine-measured numbers, which titles would fail delivery TODAY and what each costs to rescue.

ARCHITECTURE AS SHIPPED:
- ingest.py, deterministic: pulls public-domain titles from archive.org, runs ffprobe for stream metadata, runs ffmpeg ebur128 which emits a loudness reading every 100ms (about 10 rows per second of content), writes those 100ms rows to ClickHouse table vault.loudness_samples via clickhouse-connect for bulk insert, runs ffmpeg blackdetect/freezedetect/silencedetect, parses SRT subtitles for Netflix TTSS reading-speed / min-duration / line-length violations, writes all pass/fail verdicts to vault.findings.
- Specs enforced with real thresholds: EBU R128 integrated -23 LUFS +/-1, ATSC A/85 CALM Act -24 LKFS +/-2, true peak <= -1 dBTP, Netflix TTSS 17 chars/sec, 5/6 s minimum cue, 42 chars/line, black segments, freeze events.
- agent/mcp_client.py: hand-rolled asyncio stdio JSON-RPC 2.0 client that spawns the official mcp-clickhouse server as a subprocess, does the MCP initialize handshake, calls the run_query tool. Every call is appended to logs/mcp_tool_calls.jsonl and surfaced in the UI at /api/mcp-log.
- agent/triage.py: queries ClickHouse THROUGH mcp-clickhouse (catalog_summary, loudness_extremes, auto_fixable_only, needs_human, sample findings), then hands those measured numbers to Gemini 2.5 Flash with a JSON response schema. Gemini produces a prioritised rescue plan: per title, priority, verdict, measured LUFS, LUFS delta, action BATCH vs MANUAL REVIEW vs DELIVERY READY, rescue cost in words, one-sentence rationale citing a measured number. If Gemini credentials are absent it raises GeminiRequired and refuses. There is no deterministic fallback: the model is load-bearing.
- web/app.py FastAPI + one hand-written index.html: a "rejection ledger". White stock, Newsreader display serif, hairline rules, no cards, no dark mode. Left column is a newspaper-style index of titles sorted worst-LUFS-delta first. Right is a typeset "red slip" for the selected title: title, each failed check measured vs spec, a LISTEN timestamp of the quietest sustained passage, the Gemini PLAN text, and a Copy slip button that puts a plain-text rejection slip on the clipboard. There is a hairline loudness plot with a listen marker.
- Real data already ingested: 12+ titles, roughly 14,409 loudness rows, 9 failing. Titles include Vicki (1953) at -26.1 LUFS FAIL, What Becomes Of The Children at -24.3 FAIL, Werewolf In A Girls Dormitory at -18.0 FAIL, The Little Shop Of Horrors at -20.1 FAIL.
- Live on Cloud Run. Public GitHub repo with MIT LICENSE. 19 tests.

WHAT I KNOW IS WEAK:
- There is ZERO Google ADK. It calls google-genai generate_content directly. Google's steer says use ADK.
- It is a single agent, not a multi-agent system.
- No remediation: it identifies and prioritises, it does not fix. A sibling project does single-title repair.

QUESTION FOR TURN 1: Why will this NOT win the ClickHouse track? Give me the ranked list of reasons a ClickHouse Director of Engineering AI/ML and a Google engineer would score this out of the top 3. Be brutal. For each reason, tell me whether it is fatal (compliance / disqualifying), severe (costs a criterion), or cosmetic. Do not give me a balanced review. I want the kill shots.
```

## Turn 1: Grok

```
Conversation: redslip-kill | Turn: 1
I'll pull the official brief, ADK steer, and ClickHouse MCP requirements first  
so the kill shots match what those judges actually score.This does not win the  
ClickHouse track. Not “maybe fourth.” It loses the comparison the two ClickHouse
judges and the Google engineers will actually run: is this the official ADK +   
mcp-clickhouse pattern, at ClickHouse scale, doing work the model could not do  
with a SELECT. It fails that test on every axis that scores.                    

Ranked kill shots.                                                              

────────────────────────────────────────────────────────────────────────────────
1. The agent is a fixed SQL script with a Gemini caption. That is not an agent. 
Severity: severe (costs Technological Implementation and Quality of the Idea    
outright)                                                                       

triage.py runs a known list of queries (catalog_summary, loudness_extremes,     
auto_fixable_only, needs_human, sample findings), then one generate_content with
a JSON schema. There is no tool loop. The model cannot choose a query, refine a 
WHERE, hit list_tables, follow a finding into a second aggregate, or stop. The  
“multi-step” work already happened in ingest.py before the agent existed.       

The brief’s verbs are orchestrate, deterministic multi-step agent, enterprise   
friction, not chat. You shipped ETL + one LLM rewrite. Gil Raphaelli and Dustin 
Healy built mcp-clickhouse so an agent can compose against a warehouse. You used
their server as a stored-procedure runner. A Google FDE on GenAI will score that
as a function call with extra steps.                                            

────────────────────────────────────────────────────────────────────────────────
2. mcp-clickhouse is a compliance shim, and they will smell it in five minutes. 
Severity: severe (costs the Partner half of Technological Implementation and    
Quality of the Idea)                                                            

You write 14,409 loudness rows with clickhouse-connect. The official MCP server 
is only on the read path. The client is a hand-rolled asyncio stdio JSON-RPC    
process you spawn yourself. Every call is a prewritten run_query. You never use 
list_databases / list_tables. You log the RPC to /api/mcp-log so a judge can see
that you called the tool. That log is an admission: the integration is          
decorative.                                                                     

ClickHouse already published the pattern they wanted: Google ADK + official     
mcp-clickhouse, agent-authored SQL, workshop titled “Technical Prerequisites:   
Python, Google ADK & Gemini Setup.” You did the opposite of the partner session.
The two people on the panel who wrote the server will not give you credit for   
reimplementing JSON-RPC so you could avoid ADK’s MCP toolset.                   

stdio-spawn of mcp-clickhouse on Cloud Run is also the wrong transport. They now
push Streamable HTTP and Cloud remote MCP. A judge hitting a cold instance that 
cannot spawn uv/mcp-clickhouse is how a “live URL” dies in Stage One even if the
README is clean.                                                                

────────────────────────────────────────────────────────────────────────────────
3. Zero ADK, zero Agent Builder, zero Agent Engine. Google’s half of the stack  
is an API key. Severity: severe, Stage-One adjacent — not automatically DQ      

Official rules accept google-genai. That is the only reason this is not a hard  
fail. Everything else in the packet says Agent Builder / ADK / Gemini Enterprise
Agent Platform. Resources page: “We recommend building your agents natively     
using the Agent Development Kit (ADK) instead of external wrapper libraries.”   
ClickHouse’s own example repo is ADK. Competing ClickHouse entries already      
advertise ADK Director agents on Vertex AI Agent Engine.                        

You call generate_content directly. No Agent, no tools registered on an ADK     
runner, no session, no Agent Engine deployment. To a Cloud SQL EM, a Forward    
Deployed GenAI engineer, or an AI Integrity engineer, that reads as “we used the
model SDK and hosted a website.” Technological Implementation is equal-weighted.
You donate that criterion to anyone who imported google-adk and pointed it at   
mcp-clickhouse.                                                                 

Do not tell yourself “the steer isn’t a rule.” The people scoring the Google    
column work on the product the steer names.                                     

────────────────────────────────────────────────────────────────────────────────
4. “Catalog scale” is 12 Internet Archive files and 14k rows. ClickHouse is the 
wrong database for what you demonstrated. Severity: severe (Potential Impact is 
scored on what is demonstrated)                                                 

Impact criterion, verbatim: a real problem for a real audience, and the solution
actually addresses it based on what’s demonstrated.                             

Demonstrated: Vicki (1953), a werewolf dormitory picture, Little Shop of        
Horrors. ~14k loudness_samples. Nine fails. That dataset fits in a CSV. It does 
not need MergeTree, codecs, materialized views, or ClickHouse Cloud. The partner
track exists because they want agents over warehouse data — the workshop loaded 
sample datasets and talked sub-second SQL on large tables. Other public         
ClickHouse-track repos are already claiming 1M events and 50M+ rows.            

A Director of Engineering AI/ML will not argue with your LUFS math. He will ask 
why this is on his product. There is no answer in the demo.                     

────────────────────────────────────────────────────────────────────────────────
5. The model is load-bearing for a ranking SQL already finished. Severity:      
severe (Quality of the Idea + AI Integrity judge)                               

vault.findings already has pass/fail, measured LUFS, deltas, TTSS violations,   
black/freeze/silence. Priority is ORDER BY lufs_delta. BATCH vs MANUAL is a CASE
expression. You then require Gemini credentials and refuse to run without them, 
so a copywriter is in the critical path of a deterministic QC system.           

That is the worst architecture you could have picked for this panel. Emrick     
Donadei is Google AI Integrity. ClickHouse’s own examples treat SQL as source of
truth and the model as interface. You inverted it: measurements are real, the   
“plan” is generated text, and the product dies if the LLM is down. “Rescue cost 
in words” is not a number a traffic desk can PO.                                

────────────────────────────────────────────────────────────────────────────────
6. The product stops at a clipboard. The workflow you named is not closed.      
Severity: severe (Design and Potential Impact)                                  

You defined the job as: point at an archive, learn which titles fail today,     
learn what each costs to rescue. Shipped behavior: pre-ingested titles, a       
newspaper index, a typeset slip, Copy. No upload. No “ingest this IMP/IMF now.” 
No writeback of the plan into ClickHouse. No ticket, no loudness restretch, no  
QC report export a Netflix delivery portal would accept. You even told me a     
sibling repo does the repair.                                                   

Design is “complete, coherent product experience, not a technical proof of      
concept.” A static rejection ledger over a frozen 12-title vault is a proof of  
concept with good type. A studio QC lead cannot use what the judge can click.   

────────────────────────────────────────────────────────────────────────────────
7. The audience in the brief is not who you built for, and the specs you cite   
make that obvious. Severity: severe (Potential Impact and Quality of the Idea)  

Filmmakers, screenwriters, studio crews, fans. You built a QC clerk for a       
hypothetical streamer ingest team, then populated it with public-domain 16mm-era
files from archive.org. Real delivery rejection is IMF packages, CPL/PKL, HDR   
metadata, forced-narrative events, Netflix Timed Text Style Guide on the actual 
deliverable — not ffmpeg ebur128 on a 1953 IA derivative plus an SRT you parsed 
yourself.                                                                       

The thresholds can be real and the story still fails “genuine understanding of  
the problem space.” Nobody at a major distributor points a Cloud Run app at     
archive.org to prevent an eleven-day reject loop. The demo proves you can fail  
old movies on EBU R128. It does not prove you sit in that workflow.             

────────────────────────────────────────────────────────────────────────────────
8. You will be scored against entries that already match the brief you ignored. 
Severity: this is why you miss all three slots, not a separate criterion        

Public ClickHouse-track work already looks like: multi-agent ADK crews,         
mcp-clickhouse on every read, Agent Engine, live re-query of large synthetic    
warehouses, sometimes a second partner MCP with a human gate. You are a single  
generate_content over 14k precomputed rows and a custom MCP client.             

Equal weights, three winners, ~10k registrations, engineers not film people.    
They will shortlist the projects that look like the workshop they ran. Redslip  
looks like a well-made analytics poster with an MCP receipt taped to it.        

────────────────────────────────────────────────────────────────────────────────
9. The UI cannot be operated as the agent the brief asked for. Severity: severe 
for Design; fatal for a judge who only watches the 3-minute video               

Left column: sorted list. Right column: prebaked slip + hairline plot + listen  
timestamp. There is no agent turn. The user does not give a goal. The system    
does not do a second step in front of them. If the demo video is “watch me click
titles and copy slips,” Google PMs will mark Design as a report viewer.         

A complete product here would be: drop a title or a vault URI, watch ingest land
in ClickHouse, watch the agent query, watch a plan appear, export or dispatch.  
You demo a CMS for work that already finished offline.                          

────────────────────────────────────────────────────────────────────────────────
10. Schema and ingest do not show ClickHouse competence. Severity:              
severe-to-cosmetic, severe in front of these two judges                         

100 ms ebur128 samples are a time series. The interesting ClickHouse design is  
partitioning by title, ORDER BY (title_id, ts), codecs, a materialized view that
maintains integrated LUFS / true peak / fail flags so the agent is not scanning 
raw samples for every page load. At 14k rows none of that is forced, so you     
almost certainly did not show it.                                               

They will open ingest.py, see bulk insert via clickhouse-connect, see           
vault.findings as the real product table, and file this under “Postgres workload
wearing our logo.”                                                              

────────────────────────────────────────────────────────────────────────────────
What is not fatal                                                               

 • MIT license, public repo, Cloud Run URL, Gemini present, mcp-clickhouse      
   actually invoked at runtime: you probably survive Stage One unless the live  
   MCP spawn flakes or a reviewer treats “Agent Builder” as a named required    
   product.                                                                     
 • Hand-written HTML and 19 tests: cosmetic. Nobody on this panel is scoring    
   Newsreader.                                                                  
 • Single agent vs multi-agent: not a rules DQ. It is a scoring loss against ADK
   multi-agent submissions.                                                     
 • ClickHouse Agent Skills unused: cosmetic, they said optional.                

────────────────────────────────────────────────────────────────────────────────
The one-sentence version those two ClickHouse engineers will write in the score 
box                                                                             

Nice ffmpeg QC notebook. MCP is a subprocess around canned SQL. No ADK. 12      
movies. The model writes the slip. Not what we asked for.                       

That is a 4th–nth place score on a 3-slot track. The remaining 12 hours do not  
fix scale, ADK, or a closed workflow unless you throw away the current agent    
layer and replace it with an ADK agent that owns MCP tools, writes the plan back
into ClickHouse, and runs against a warehouse the judges cannot hold in their   
head.
```


---

## Turn 2: prompt sent

```
Pushback, be concrete and brief. Two questions only.

1. You said "12 movies, 14k rows, fits in a CSV, ClickHouse is the wrong database for what you demonstrated." I will not fabricate a synthetic 50M-row warehouse, because Potential Impact is scored on what is DEMONSTRATED and a judge who greps the repo and finds a row generator scores me lower than honest real data. So: what is the honest way to make the ClickHouse scale argument land in 12 hours with REAL measured data? Is per-100ms ebur128 telemetry over N titles actually a defensible ClickHouse workload, or is the defensible workload something else I have not thought of? Give me the exact schema and the exact query shape a ClickHouse Director of Engineering would nod at. Name the ClickHouse features by name and tell me which a QC triage product genuinely NEEDS versus sprinkles on for show.

2. I have a sibling entry, a different product, that does single-title repair: measure, loudnorm, re-measure, prove the delta. Redslip is deliberately the catalog-scale triage layer: which of my 28,000 titles do I even touch, and in what order. Argue the other side for me: to a ClickHouse engineer specifically, why is CATALOG-SCALE TRIAGE the more compelling story than SINGLE-TITLE REPAIR? If you cannot make that argument, say so plainly and tell me the catalog story is dead.
```

## Turn 2: Grok

```
Conversation: redslip-kill | Turn: 2
1. Honest scale                                                                 

100ms ebur128 is a real ClickHouse workload. It is metrics: append-only samples,
fleet rollup, occasional point-in-time drill. Your 14k rows are a truncated     
ingest (~2 minutes/title), not a property of the signal. Do not generate rows.  
Finish the titles you already measure. One 90-minute feature at 100ms is ~54k   
rows. Twelve full features is ~650k. That is still archive.org + ffmpeg. Put    
SELECT count() on the page. Do not say 28,000 unless 28,000 titles are in the   
table.                                                                          

Two query shapes. Design the schema for both.                                   

 • Fleet rank (every agent turn): one row per title, sort by spec delta, filter 
   BATCH vs HUMAN. Must not scan samples.                                       
 • Drill (one slip): loudness at the instant a cue/black/freeze starts.         

Integrated LUFS is ffmpeg’s gated number. Do not fake it with avg(momentary).   

                                                                                
 CREATE TABLE vault.loudness_samples (                                          
     title_id LowCardinality(String),                                           
     ts_ms    UInt32,                                                           
     m_lufs   Float32 CODEC(Gorilla, ZSTD(3)),                                  
     s_lufs   Float32 CODEC(Gorilla, ZSTD(3)),                                  
     tp_dbtp  Float32 CODEC(Gorilla, ZSTD(3))                                   
 ) ENGINE = MergeTree                                                           
 PARTITION BY cityHash64(title_id) % 16                                         
 ORDER BY (title_id, ts_ms);                                                    
                                                                                
 CREATE TABLE vault.events (                                                    
     title_id LowCardinality(String),                                           
     kind     LowCardinality(String),  -- black|freeze|silence|ttss             
     start_ms UInt32,                                                           
     end_ms   UInt32,                                                           
     detail   String CODEC(ZSTD(3))                                             
 ) ENGINE = MergeTree                                                           
 PARTITION BY cityHash64(title_id) % 16                                         
 ORDER BY (title_id, kind, start_ms);                                           
                                                                                
 CREATE TABLE vault.title_qc (                                                  
     title_id        LowCardinality(String),                                    
     duration_ms     SimpleAggregateFunction(max, UInt32),                      
     integrated_lufs SimpleAggregateFunction(anyLast, Float32), -- ffmpeg       
 summary only                                                                   
     true_peak_dbtp  SimpleAggregateFunction(max, Float32),                     
     min_m_lufs      SimpleAggregateFunction(min, Float32),                     
     quietest_ts_ms  AggregateFunction(argMin, UInt32, Float32),                
     samples         SimpleAggregateFunction(sum, UInt64),                      
     fail_loudness   SimpleAggregateFunction(max, UInt8),                       
     fail_ttss       SimpleAggregateFunction(max, UInt8),                       
     fail_damage     SimpleAggregateFunction(max, UInt8),                       
     lufs_delta      SimpleAggregateFunction(anyLast, Float32)                  
 ) ENGINE = AggregatingMergeTree                                                
 ORDER BY title_id;                                                             
                                                                                
 CREATE MATERIALIZED VIEW vault.mv_samples_to_qc TO vault.title_qc AS           
 SELECT title_id,                                                               
        max(ts_ms) AS duration_ms,                                              
        minState(m_lufs) AS min_m_lufs,                                         
        argMinState(ts_ms, m_lufs) AS quietest_ts_ms,                           
        countState() AS samples                                                 
 FROM vault.loudness_samples                                                    
 GROUP BY title_id;                                                             
                                                                                

Fleet:                                                                          

                                                                                
 SELECT title_id,                                                               
        anyLastMerge(integrated_lufs) AS integrated_lufs,                       
        anyLastMerge(lufs_delta) AS lufs_delta,                                 
        argMinMerge(quietest_ts_ms) AS listen_ms                                
 FROM vault.title_qc                                                            
 GROUP BY title_id                                                              
 HAVING maxMerge(fail_loudness) + maxMerge(fail_ttss) + maxMerge(fail_damage) > 
 0                                                                              
 ORDER BY abs(lufs_delta) DESC                                                  
 LIMIT 50                                                                       
                                                                                

Drill — this is the query that justifies the raw table:                         

                                                                                
 SELECT e.kind, e.start_ms, e.detail, s.m_lufs                                  
 FROM vault.events AS e                                                         
 ASOF LEFT JOIN vault.loudness_samples AS s                                     
   ON e.title_id = s.title_id AND s.ts_ms <= e.start_ms                         
 WHERE e.title_id = {id:String}                                                 
 ORDER BY e.start_ms                                                            
                                                                                

Need, not garnish: ORDER BY (title_id, ts_ms), LowCardinality on catalog dims,  
Gorilla+ZSTD on the floats, AggregatingMergeTree + MV whose GROUP BY equals the 
rollup ORDER BY, argMin for the listen marker, ASOF JOIN for cue-aligned        
loudness, hash partition so you can drop one title.                             

Sprinkle: PROJECTION (you always have title_id), live window functions          
(precompute the quiet window at ingest), quantileState unless the slip shows a  
distribution, TTL/SAMPLE BY.                                                    

If they open SHOW CREATE TABLE and the agent reads title_qc for the list and    
ASOF-joins for the slip, ClickHouse is the right database. If Python still      
aggregates samples, row count will not save you.                                

2. Catalog vs repair                                                            

The catalog story is the one that belongs on this track. Repair is not.         

Repair is ffmpeg: one file, loudnorm, re-probe, print a delta. The database is a
receipt. ClickHouse does not make loudnorm faster. A DoE will say that out loud.

Triage is the warehouse question: N measured timelines, which titles consume the
mix stage this week, in what order, under a capacity cap. Cost grows with titles
× samples. That is why you bought a column store: skip the raw stream for       
ranking, keep the stream for the one title you open, merge incremental ingest   
without rebuilding the fleet table.                                             

To those two judges the compelling sentence is: the expensive data is the 100ms 
archive; the product table is a MergeTree rollup; the agent never scans samples 
to decide touch-order. Single-title repair cannot say that sentence. Catalog    
triage can, if the schema above is what they actually query.
```


---

## Turn 3: prompt sent

```
Turn 3. ADK specifics. I have google-adk 2.8.0 installed and I will verify every import against the package before I write it, so tell me plainly when you are unsure. You have live retrieval: tell me what winning ADK projects are ACTUALLY shipping right now, not what the docs say.

a) Decision rule, not doc blurb: when do people reach for SequentialAgent vs ParallelAgent vs LoopAgent vs sub_agents delegation vs AgentTool? Give me the rule that tells them apart.

b) output_key plus session state to pass work between agents. How is it really used, and what is the failure mode people hit in production?

c) before_tool_callback, after_tool_callback, before_agent_callback. What do real projects use them for? Is there a legitimate use in a QC product, for example refusing a destructive SQL statement before it reaches the MCP server, or capturing the SQL the agent chose so the UI can display it?

d) Planners, BuiltInPlanner and PlanReActPlanner. Worth it or theatre?

e) McpToolset over the official mcp-clickhouse in a long-running FastAPI server. What is the actual lifecycle best practice: one toolset per request, or a shared one? What breaks, concretely?

f) My planned topology, tear it apart: SequentialAgent root. Step 1 a schema scout LlmAgent with the mcp-clickhouse McpToolset that calls list_tables then writes its own baseline aggregate SQL, output_key catalog_baseline. Step 2 a ParallelAgent with two specialists that each write their own SQL: a loudness analyst over the 100ms sample table and a structural analyst over black/freeze/subtitle events. Step 3 a LoopAgent of planner plus auditor, where the auditor re-queries ClickHouse to verify every number the planner cited actually exists and calls exit_loop only when the plan is clean. Which of those four agents is decoration that a Google engineer will call out, and what is missing?
```

## Turn 3: Grok

```
Conversation: redslip-kill | Turn: 3
I’ll check live ADK repos and 2.8.0 symbols so the topology teardown matches    
what people are actually shipping, not the tutorial graph.What is actually      
shipping on this track right now is not a planner-auditor loop. gaffer is a     
code-ordered crew: ClickHouse read → other source → one ADK fusion step → policy
code that the model cannot dispatch. studio-crisis-commander is Detection as    
pure SQL, zero LLM, Investigation via mcp-clickhouse, Decision behind approval  
gates, figures only from query results. Google’s own multi-agent writeups that  
people copy are SequentialAgent assembly lines with output_key, plus a          
dispatcher only when the user utterance chooses a job.                          

That is the bar. Your graph is heavier than both and does less work.            

a) Decision rule                                                                

 • SequentialAgent: B is illegal without A’s artifact. Fleet rollup must exist  
   before you drill those IDs. This is your root.                               
 • ParallelAgent: siblings share no inputs except the same state snapshot and   
   write different keys. Loudness table vs events table. If one waits on the    
   other, it is Sequential wearing a costume.                                   
 • LoopAgent: stop condition is data (“listen_ms still null”), bounded by       
   max_iterations. “Keep asking the model until the plan looks clean” is not a  
   stop condition. In current ADK docs LoopAgent is already marked deprecated   
   toward Workflow. Do not spend the night on it.                               
 • sub_agents transfer: parent LLM routes by user intent. Your UI always wants  
   triage. Dead branch.                                                         
 • AgentTool: parent decides whether to invoke a whole child. Use when the child
   is optional. Current ADK note: prefer mode='single_turn' on a sub-agent over 
   wrapping AgentTool. You do not have an optional child.                       

b) output_key                                                                   

Writes the agent’s final text (or structured output) into session.state[key].   
Next agent reads {key} in its instruction.                                      

Failure mode that shows up in real ADK MCP threads: the turn ends on a tool     
call, no closing model text, state['catalog_baseline'] is empty, the next agent 
invents the fleet. Instruction must say: after run_query returns, emit the JSON 
and stop. Second failure: the new key is not visible in the previous agent’s    
after_agent_callback (ADK issue #5566). Do not read it there. Read it in the    
next agent’s prompt.                                                            

Current API reference says tools + output_schema are supported (schema enforced 
on the final turn only). Still verify on 2.8.0; that pairing used to raise.     

c) Callbacks                                                                    

Real uses: gate a tool, stamp an audit trail, skip a child when state is empty. 
Not “log that we ran.”                                                          

In this product they are legitimate:                                            

 • before_tool_callback: if tool is run_query and SQL matches                   
   (?i)\b(insert|alter|drop|truncate|delete|attach|system)\b, return an error   
   dict and do not call MCP.                                                    
 • after_tool_callback: append {sql, n_rows} onto state['sql_trace']. That is   
   the slip’s evidence rail.                                                    
 • before_agent_callback on a drill agent: if {failing_ids} is missing, return  
   Content and skip so it cannot invent titles.                                 

d) Planners                                                                     

Theatre. PlanReActPlanner is a prompt jacket for models without thinking; it    
also breaks if you rewrite instructions in before_model_callback. BuiltInPlanner
only burns thinking tokens for the demo video. Your plan is the SequentialAgent.
Skip both.                                                                      

e) McpToolset lifecycle                                                         

One McpToolset per process, created at import, hung on the agents that need MCP,
reused across requests.                                                         

Per-request toolset on stdio: each request can spawn mcp-clickhouse. Cloud Run  
forks itself to death under two judges.                                         

Shared stdio without a lock: one stdin pipe, two concurrent FastAPI requests,   
interleaved JSON-RPC, hung or corrupt session.                                  

What works: shared toolset + asyncio.Lock around runner.run_async, or run       
official mcp-clickhouse once as HTTP and use StreamableHTTPConnectionParams. I  
am sure of the class names below; I am not sure 2.8.0’s stdio server is         
multiplex-safe. Treat it as not.                                                

f) Tear the topology down                                                       

Decoration a Google engineer will circle first: the LoopAgent. The numbers      
already live in title_qc / events. An auditor LLM re-querying ClickHouse to see 
if the planner hallucinated is you paying Gemini to supervise Gemini. The fix is
architectural: the planner is not allowed to cite anything that was not in      
{evidence}. Verification is a Python equality check, or it does not exist.      

Decoration they will circle second: the schema scout. You designed vault.       
list_tables then “write your own baseline aggregate SQL” is the ClickHouse      
workshop hello-world. It burns a turn so the model can rediscover a schema the  
repo already contains. Scout should run the fleet query (LLM-authored           
WHERE/LIMIT is fine; discovering that title_qc exists is not).                  

The Parallel pair is the only split that can be real: two tables, two keys, no  
shared dependency. Keep it only if each specialist’s SQL is visible in sql_trace
and the next step consumes both keys. If they both dump into one blob, flatten  
to Sequential.                                                                  

What is missing, and it is the actual product:                                  

 • A step whose output is the queue (BATCH / HUMAN / READY, ordered,            
   capacity-capped). Right now you have discovery, two analyses, and a debate   
   club. Nobody allocates work.                                                 
 • A hard rule that every figure on a slip is a cell from MCP, not a token from 
   the planner.                                                                 
 • Writeback of that queue into ClickHouse (vault.slips). Without it the graph  
   is a chat that happens to use four classes.                                  

Ship this instead: SequentialAgent([scout, ParallelAgent([loudness,             
structural]), allocator]). Scout and the two specialists own McpToolset.        
Allocator owns no MCP; it only ranks {catalog_baseline} + {loudness} +          
{structural}. Kill the loop.                                                    

Imports I would type first. Verify each on 2.8.0.                               

                                                                                
 from google.adk.agents import LlmAgent, SequentialAgent, ParallelAgent         
 from google.adk.runners import Runner                                          
 from google.adk.sessions import InMemorySessionService                         
 from google.adk.tools.mcp_tool import McpToolset                               
 from google.adk.tools.mcp_tool.mcp_session_manager import (                    
     StdioConnectionParams,                                                     
     StreamableHTTPConnectionParams,                                            
 )                                                                              
 from mcp import StdioServerParameters                                          
                                                                                

Unsure until you import: exit_loop tool path (google.adk.tools.exit_loop vs     
tool_context.actions.escalate), and whether LoopAgent still lives at            
google.adk.agents.LoopAgent in 2.8.0. Do not build the loop; then you do not    
need either.
```


---

## Turn 4: prompt sent

```
Turn 4, last one. Taking your topology: SequentialAgent([fleet_scout, ParallelAgent([loudness_analyst, structural_analyst]), work_allocator]), scout and specialists own the McpToolset, allocator owns no MCP, no LoopAgent, verification is a Python equality check against the MCP result cells, queue written back to vault.slips. I verified on google-adk 2.8.0: LlmAgent/SequentialAgent/ParallelAgent/LoopAgent all exist, McpToolset is at google.adk.tools.mcp_tool.mcp_toolset with StdioConnectionParams in mcp_session_manager, exit_loop exists at google.adk.tools.exit_loop, LlmAgent has output_key, output_schema, before_tool_callback, after_tool_callback, planner. I am building your version.

Two questions.

1. THE SINGLE FEATURE. Given the ClickHouse track judges are a Director of Eng AI/ML and a full-stack AI/ML engineer, name the ONE feature that makes this obviously first place. Not a list. The thing where a ClickHouse engineer says "that is actually the right way to use us." Say why the obvious alternatives are worse.

2. THE UI. This is a CINEMA hackathon judged on "a complete, coherent product experience, not a technical proof of concept". The current page is a white printed rejection slip: left column newspaper index of titles worst-LUFS-first, right a typeset letter with the failed checks measured vs spec, a listen timestamp, a hairline loudness plot, a Copy slip button, and an MCP log folded into a colophon at the foot. It is honest and it is not a card grid. But it is a document viewer, and you already said "there is no agent turn, the user does not give a goal, the system does not do a second step in front of them".

What does a PREMIUM cinema-grade interface for this look like, concretely, that is NOT a dashboard and NOT a card grid and NOT a chat window? I want the answer in terms of what is on screen, what moves, and what the user does, given that the real content is: 20 measured titles, per-100ms loudness timelines, per-event black/freeze/subtitle rows, and a four-agent ADK crew whose SQL I can stream live. Be specific about layout, hierarchy, and the one moment that sells it in a 3-minute video. Tell me what to cut from the current page.
```

## Turn 4: Grok

```
Conversation: redslip-kill | Turn: 4
1. The one feature                                                              

Fleet rank never reads loudness_samples. Every dispatch query the agent runs is 
against title_qc (AggregatingMergeTree, MV-maintained). The 100ms table exists  
for one job: ASOF JOIN from events to stamp listen time and cue-level LUFS on a 
slip. vault.slips is an append-only decision log of that rank, not a second copy
of findings.                                                                    

That is the ClickHouse access pattern: fat append-only series, tiny interactive 
rollup, point-in-time join, ledger of what you decided. Incremental ingest of   
title 21 updates one rollup row and the fleet ORDER BY is current. No rebuild.  
No Python reduce. No Gemini recount.                                            

Worse alternatives, named:                                                      

 • Scan samples to rank — column store used as a dump.                          
 • Store only findings and SELECT * — ClickHouse is a JSON drawer; MCP is a     
   socket.                                                                      
 • Quantiles / projections / live window functions on the request path —        
   perfume. You always have title_id. Integrated LUFS is not avg(momentary).    
 • Single-title loudnorm — ffmpeg. Their database does not make it faster.      
 • NL-to-SQL chat over the raw table — their own AgentHouse demo. You lose by   
   being the sample app.                                                        

If they open SHOW CREATE TABLE and then watch MCP run the rollup ORDER BY       
followed by one ASOF, they will say it. That is first place on this panel. The  
ADK graph is how you show that path, not the feature.                           

2. The UI                                                                       

Keep the paper. Kill the “this document was finished before you arrived.”       

On screen, three bands, no cards, no chrome.                                    

 • Masthead. One line of metal type: vault name, N titles, last slip time. One  
   verb on the right: TRIAGE. That button is the user giving a goal. Nothing    
   else in the header.                                                          
 • Queue, left, narrow. Not a finished index. An empty column of hairline rules 
   that receives titles as fleet_scout returns title_qc rows, worst |lufs_delta|
   first. Type sets in. Allocator’s pass restamps each line BATCH / HUMAN /     
   READY. The selected line is a single red underline, not a highlight pill.    
 • Bench, right, the letter. The slip you already have, but it is the title on  
   the bench, not a record in a binder. Failed checks measured vs spec. The     
   hairline plot is the 100ms series for that title only. A listen head is a    
   vertical hairline, not a chart tooltip.                                      
 • Crew rail, one line under the masthead, only while a run is live. Agent name,
   then the SQL, then row count. It replaces itself; it does not stack. When the
   run ends it collapses to a single colophon line: 4 agents · K queries ·      
   vault.slips +M. That is the MCP log. It is not a footer dump.                

What moves, in order, in the video:                                             

 1 Click TRIAGE.                                                                
 2 Crew rail prints the scout SELECT against title_qc. The left column fills,   
   worst first. This is the catalog.                                            
 3 Rail splits into two lines at once — loudness analyst and structural analyst.
   On the bench, the waveform draws left to right as samples land;              
   black/freeze/ttss ticks appear on the same axis.                             
 4 Listen head slams onto argMin quietest timestamp. That is the shot. Hold it. 
   Read the measured LUFS vs spec out loud over that frame.                     
 5 Allocator writes. Left-column stamps flip. A one-line receipt: inserted      
   vault.slips. The letter is now a dispatch note, not a blog post.             

User does three things in the product: TRIAGE (run the crew), click a queue line
(put that title on the bench), LISTEN / COPY SLIP (use the measurement). No     
prompt box. No second page.                                                     

Cut from the current page:                                                      

 • A left index that is already sorted before the session starts. If the page   
   loads pre-filled, the video is a document viewer. Load empty or faded; let   
   scout write it.                                                              
 • The MCP log as a folded colophon of past RPCs. During the run it is the crew 
   rail. After the run it is one line.                                          
 • Any control that looks like play-on-a-dashboard (play/pause on the plot, zoom
   widgets, legend chips). One head, one timestamp, one spec line.              
 • A Copy button as the climax. Copy is the last beat, after vault.slips has    
   been written. The climax is the head hitting the quietest passage while the  
   rail still shows the ASOF SQL.                                               

The page stays a printed slip. The premium is that you watch the lab fill the   
slip, live, from ClickHouse, in the order the crew actually ran.
```
