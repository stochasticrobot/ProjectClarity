/* Display one requested entity with cited overview, sections and identity uncertainty.
 * All model/source text remains literal text. This renderer does not merge entities,
 * hide missing sections, contact providers or change the saved report artifact.
 */
'use strict';
/** Render a validated profile using the report page's shared citation controls.
 * Args: report — validated artifact; container — destination element;
 * makeNode — literal-text DOM factory; cite — helper linking facts to source cards.
 * Returns: nothing; replaces only the profile body, retaining all section statuses.
 */
function renderEntityProfile(report,container,makeNode,cite){
 container.replaceChildren();
 const profile=report.profile,header=makeNode('section','');
 header.className='report-entry';
 header.append(makeNode('h2',profile.subject),makeNode('p',`Requested ${profile.subject_kind} · ${profile.subject_scope??'No further identity qualifier supplied'}`));
 header.append(makeNode('p','The identity above describes the requested subject. Findings below are limited to the cited sources.'));
 const overview=makeNode('section','');overview.append(makeNode('h3','Overview'));
 for(const finding of report.summary){const p=makeNode('p',finding.text);p.append(makeNode('small',` — ${finding.status}`));cite(p,finding.fact_ids);overview.append(p);}
 if(!report.summary.length)overview.append(makeNode('p','Not established from the collected evidence.'));
 header.append(overview);container.append(header);
 for(const section of profile.sections){
  const card=makeNode('section','');card.className='report-entry';
  card.append(makeNode('h2',section.title),makeNode('p',`Evidence status: ${section.status}`));
  if(!section.findings.length)card.append(makeNode('p','Not established from the collected evidence.'));
  for(const finding of section.findings){const p=makeNode('p',finding.text);p.append(makeNode('small',` — ${finding.status}`));cite(p,finding.fact_ids);card.append(p);}
  container.append(card);
 }
 if(profile.identity_uncertainty.length){
  const card=makeNode('section','');card.className='report-entry';card.append(makeNode('h2','Identity uncertainty'));
  card.append(makeNode('p','These source assessments did not establish an unambiguous connection to the requested subject. Their findings were withheld.'));
  for(const item of profile.identity_uncertainty){const p=makeNode('p',item.reason);if(item.failure_categories?.length)p.append(makeNode('span',' Support issues: '+item.failure_categories.map(c=>c.replaceAll('_',' ')).join(', ')));cite(p,[item.fact_id]);card.append(p);}
  container.append(card);
 }
}
