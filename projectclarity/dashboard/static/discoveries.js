'use strict';
/** Remember which supported findings this browser tab has already announced.
 * A designed report notifies when a report entry first reaches supported status;
 * a run without a report specification notifies for supported claims with intact
 * evidence. For example, another supporting claim about an already announced
 * entry must not trigger a duplicate entry notification. On first load or a run
 * switch, existing findings establish a silent baseline. This class updates
 * tab-local notification memory only; it neither reviews evidence nor saves
 * research results. app.js calls it when a new dashboard snapshot arrives.
 */
class DiscoveryTracker {
  /** Initialize notification state without reading or modifying research receipts.
   * Args:
   *   storage: Optional sessionStorage-compatible object; unavailable storage
   *     leaves notification tracking in memory for this page only.
   * Returns:
   *   A new tracker with no selected run and an empty set of observed support IDs.
   */
  constructor(storage) { this.storage=storage; this.run=null; this.seen=new Set(); }
  /** Recover this tab's previously observed support IDs for one run.
   * Args:
   *   id: Run identifier used in the clarity-treasure storage key.
   * Returns:
   *   A Set of stored IDs, or an empty Set if storage is absent, inaccessible or
   *   contains invalid JSON. Does not fetch evidence or change research state.
   */
  read(id) { try { return new Set(JSON.parse(this.storage?.getItem('clarity-treasure:'+id)||'[]')); } catch { return new Set(); } }
  /** Save notification memory so page reloads do not repeat discoveries.
   * Args:
   *   None. Uses this.run, this.seen and the optional storage object.
   * Returns:
   *   Nothing. Writes only tab-local storage; unavailable/full storage is ignored
   *   and in-memory tracking remains active. No research receipts are changed.
   */
  save() { try { this.storage?.setItem('clarity-treasure:'+this.run,JSON.stringify([...this.seen])); } catch {} }
  /** Find supported entries or claims not previously announced in this tab.
   * Args:
   *   data: Current dashboard snapshot with run id, claims and optional report
   *     specification/target rows. Claim-only notifications require evidence_intact;
   *     designed reports use the backend's per-entry supported status.
   * Returns:
   *   Array of newly supported report-entry descriptors (including result_id and
   *   goal_id), or claim objects for a run without a report specification. First
   *   load and run changes return an empty array after saving the baseline. Updates
   *   this.run, this.seen and optional tab storage. Narrative sections do not
   *   generate automatic completion notifications; no new review is performed.
   */
  observe(data) {
    const sections=new Set((data.answer_schema?.sections||[]).map(s=>s.goal_id));
    const supported=data.answer_schema?(data.targets||[]).filter(g=>sections.has(g.goal_id)).flatMap(g=>g.rows.filter(r=>r.status==='supported').map(r=>({id:'report:'+g.goal_id+':'+r.id, result_id:r.id,goal_id:g.goal_id,statement:r.label}))):data.claims.filter(c=>c.status==='supported'&&c.evidence_intact);
    if(this.run!==data.id) { this.run=data.id;this.seen=this.read(data.id);supported.forEach(c=>this.seen.add(c.id));this.save();return []; }
    const fresh=supported.filter(c=>!this.seen.has(c.id));
    supported.forEach(c=>this.seen.add(c.id));this.save();return fresh;
  }
}
if(typeof module!=='undefined')module.exports={DiscoveryTracker};
