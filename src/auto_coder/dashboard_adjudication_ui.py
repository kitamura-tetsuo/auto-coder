"""NiceGUI review-adjudication surface backed only by the authenticated API."""

from __future__ import annotations

import json

from nicegui import ui


def register_adjudication_page(repo_name: str) -> None:
    """Register the separate PR-only operator page.

    All authoritative reads and writes remain in ``AdjudicationWriteService``.
    The browser keeps only the operator session cookie, CSRF proof, current draft,
    and unsent rationale; the GitHub credential never crosses this boundary.
    """

    @ui.page("/adjudication/pr/{pr_number}")
    def adjudication_page(pr_number: int) -> None:
        ui.label("GitHub Review Adjudication").classes("text-2xl font-bold")
        ui.label(f"Repository: {repo_name} · Pull request #{pr_number}").classes("text-sm text-gray-500")
        ui.label("This GitHub-backed operator surface is separate from locally recorded diagnostic executions.").classes("text-sm text-amber-700")
        ui.link("Back to local PR diagnostics", f"/detail/pr/{pr_number}").classes("text-blue-600")
        ui.html(
            """
            <div id="adj-root" class="q-mt-md" style="max-width:1100px">
              <div id="adj-setup" role="status">Checking authoring configuration…</div>
              <section id="adj-login" hidden>
                <label for="adj-secret"><b>Operator secret</b></label><br>
                <input id="adj-secret" type="password" autocomplete="current-password" style="border:1px solid #999;padding:8px;margin:8px 0">
                <button id="adj-login-button" type="button">Authenticate</button>
              </section>
              <section id="adj-session" hidden>
                <p id="adj-account"></p><button id="adj-logout" type="button">Log out</button>
                <p><button id="adj-refresh" type="button">Refresh authoritative GitHub observations</button></p>
                <p id="adj-publication" role="status"></p>
                <div id="adj-findings"></div>
              </section>
              <p id="adj-error" role="alert" style="color:#b91c1c;white-space:pre-wrap"></p>
            </div>
            """,
            sanitize=False,
        )
        script = r"""
        (() => {
          const pr = __PR__;
          const repo = __REPO__;
          const api = '/dashboard-adjudication';
          let csrf = sessionStorage.getItem(`adj-csrf-${pr}`) || '';
          let generation = 0;
          let connected = true;
          let refreshController = null;
          let publisherDescription = 'unavailable';
          const rationales = new Map();
          const attemptKey = `adj-publication-${pr}`;
          const byId = id => document.getElementById(id);
          const error = message => { byId('adj-error').textContent = message || ''; };
          async function request(path, options={}) {
            options.headers = {...(options.headers || {}), 'Content-Type':'application/json'};
            if (csrf) options.headers['X-CSRF-Token'] = csrf;
            const response = await fetch(api + path, options);
            const body = await response.json().catch(() => ({}));
            if (!response.ok) {
              const failure = new Error(typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail || body));
              failure.status = response.status; throw failure;
            }
            return body;
          }
          window.addEventListener('beforeunload', () => {
            connected = false;
            if (refreshController) refreshController.abort();
          });
          const line = (parent, label, value) => {
            const p = document.createElement('p');
            const strong = document.createElement('b'); strong.textContent = label + ': ';
            p.append(strong, document.createTextNode(value ?? 'unavailable')); parent.appendChild(p);
          };
          async function boot() {
            try {
              const available = await request('/availability');
              byId('adj-setup').textContent = `Configuration: ${available.configured ? 'valid' : 'unavailable'} — ${available.diagnostic}`;
              if (!available.configured) return;
              try { await loadSession(); } catch (_) { byId('adj-login').hidden = false; }
            } catch (exc) { error(exc.message); }
          }
          async function loadSession() {
            const session = await request('/session');
            byId('adj-login').hidden = true; byId('adj-session').hidden = false;
            const account = session.publisher || {};
            publisherDescription = `${account.login || 'login unavailable'} (GitHub ID ${account.id || 'unavailable'})`;
            byId('adj-account').textContent =
              `Server-verified publishing account: ${publisherDescription}. Configuration valid: ${session.configuration_valid}. ` +
              `Authorization: ${session.authorization_valid ? 'authorized' : 'denied'} — ` +
              `${session.authorization_reason}. Session expires within 30 minutes.`;
            await recoverPublication();
            await refresh();
          }
          async function recoverPublication() {
            const decisionId = sessionStorage.getItem(attemptKey);
            if (!decisionId) return;
            const target = byId('adj-publication');
            target.textContent = `Recovering publication status for decision ${decisionId}…`;
            try {
              const result = await request(`/status/${encodeURIComponent(decisionId)}`);
              if (!connected || sessionStorage.getItem(attemptKey) !== decisionId) return;
              target.textContent = `Recovered publication ${result.state} for decision ${decisionId}; ` +
                `comment ${result.github_comment_id || 'not yet confirmed'}. No decision was resubmitted.`;
              if (result.state === 'confirmed-published' || result.state === 'definitely-not-sent') {
                sessionStorage.removeItem(attemptKey);
              }
            } catch (exc) {
              target.textContent = `Publication status recovery requires authentication: ${exc.message}`;
            }
          }
          async function refresh() {
            const mine = ++generation; error('');
            if (refreshController) refreshController.abort();
            refreshController = new AbortController();
            try { const body = await request(`/context/${pr}`, {signal:refreshController.signal}); if (connected && mine === generation) render(body.findings || []); }
            catch (exc) { if (mine === generation) error(`Authoritative source unavailable: ${exc.message}`); }
          }
          function render(findings) {
            const host = byId('adj-findings'); host.replaceChildren();
            if (!findings.length) { host.textContent = 'No authoritative findings were returned. This is unavailable/incomplete evidence, not an empty-success result.'; return; }
            findings.forEach(finding => {
              const card = document.createElement('article'); card.style = 'border:1px solid #bbb;padding:16px;margin:12px 0';
              const title = document.createElement('h3'); title.textContent = `Root review thread ${finding.root_comment_id}`; card.appendChild(title);
              const root = document.createElement('a'); root.href=`https://github.com/${repo}/pull/${pr}#discussion_r${finding.root_comment_id}`; root.textContent='Open authoritative root comment'; root.target='_blank'; root.rel='noopener noreferrer'; card.appendChild(root);
              line(card, 'Reader-issued context identity', finding.context_id);
              line(card, 'Applicability', `${finding.status}: ${finding.reason}`);
              line(card, 'Observation freshness', finding.observation_revision);
              line(card, 'Bound revision', `head ${finding.head_sha}; base ${finding.base_sha} (${finding.base_ref}); contract ${finding.contract_digest}`);
              line(card, 'Contributing contract', (finding.contracts || []).map(c => `#${c.issue_number}: ${c.requirements.map(r => r.id).join(', ')}`).join('; '));
              line(card, 'Current tips', (finding.tips || []).join(', ') || 'none');
              const raw = document.createElement('pre'); raw.style.whiteSpace = 'pre-wrap'; raw.textContent = finding.raw_finding || 'Finding text unavailable'; card.appendChild(raw);
              const history = document.createElement('details'); const summary = document.createElement('summary'); summary.textContent = 'Append-only decision history'; history.appendChild(summary);
              (finding.decisions || []).forEach(decision => {
                const p = document.createElement('p');
                p.append(document.createTextNode(`${decision.created_at} — decision ${decision.decision_id} — ${decision.verdict}/${decision.directive} by `));
                const actor = document.createElement('a'); actor.href=decision.actor_url; actor.textContent=`GitHub ID ${decision.actor_id}`; actor.target='_blank'; actor.rel='noopener noreferrer';
                const comment = document.createElement('a'); comment.href=decision.comment_url; comment.textContent=`comment ${decision.comment_id}`; comment.target='_blank'; comment.rel='noopener noreferrer';
                p.append(actor, document.createTextNode(' at '), comment, document.createTextNode(`: ${decision.rationale}`)); history.appendChild(p);
              });
              if (!(finding.decisions || []).length) history.append(' No published decisions observed.'); card.appendChild(history);
              const processing = finding.processing;
              line(card, 'Downstream processing', processing ? `${processing.status} for decision ${processing.decision_id}` : 'No durable per-decision processing evidence; pending/not observed');
              if (!finding.retired_reason && finding.status !== 'SOURCE_UNAVAILABLE') addComposer(card, finding);
              else line(card, 'Submission unavailable', finding.retired_reason || 'Authoritative source is unavailable');
              host.appendChild(card);
            });
          }
          function addComposer(card, finding) {
            const select = document.createElement('select');
            [['UPHOLD/FIX','UPHOLD|FIX'],['OVERRULE/NO_CHANGE','OVERRULE|NO_CHANGE'],['UNDECIDED/NONE','UNDECIDED|NONE']].forEach(([text,value]) => { const option=document.createElement('option'); option.textContent=text; option.value=value; select.appendChild(option); });
            const rationale = document.createElement('textarea'); rationale.placeholder='Required rationale'; rationale.rows=4; rationale.style='display:block;width:100%;margin:8px 0';
            rationale.value = rationales.get(finding.root_comment_id) || '';
            const preview = document.createElement('button'); preview.textContent='Prepare confirmation preview'; preview.type='button';
            const output = document.createElement('pre'); output.style='white-space:pre-wrap;background:#f4f4f4;padding:8px';
            const confirm = document.createElement('button'); confirm.textContent='Confirm and publish GitHub reply'; confirm.type='button'; confirm.hidden=true;
            let draft = null;
            let previewVersion = 0;
            rationale.addEventListener('input', () => {
              rationales.set(finding.root_comment_id, rationale.value);
              previewVersion += 1;
              if (draft) { draft=null; confirm.hidden=true; output.textContent='Rationale changed. Prepare a new preview and decision identity.'; }
            });
            preview.onclick = async () => {
              if (!rationale.value.trim()) { output.textContent='A rationale is required.'; return; }
              const preparedRationale = rationale.value;
              const preparedVersion = previewVersion;
              try {
                const [verdict,directive] = select.value.split('|');
                const prepared = await request('/draft', {method:'POST',body:JSON.stringify({pr_number:pr,context_id:finding.context_id,verdict,directive,rationale:preparedRationale})});
                if (!connected || preparedVersion !== previewVersion || preparedRationale !== rationale.value) {
                  output.textContent='Rationale changed while preparing the preview. Prepare a new preview and decision identity.';
                  confirm.hidden=true; return;
                }
                draft = prepared;
                draft.verdict=verdict; draft.directive=directive; draft.rationale=preparedRationale;
                output.textContent =
                  `CONFIRM TARGET: PR #${pr}, root ${draft.root_comment_id}\n` +
                  `Publishing account: ${publisherDescription}\nDecision: ${draft.decision_id}\n` +
                  `Bound head: ${draft.head_sha}\nBound contract: ${draft.contract_digest}\n` +
                  `Supersedes: ${draft.tips.join(', ') || 'none'}\n\n` +
                  `Exact server-rendered shared-schema body:\n${draft.proposed_body}`;
                confirm.hidden=false;
              } catch (exc) { output.textContent=exc.message; }
            };
            confirm.onclick = async () => {
              if (!draft) return; confirm.disabled=true; output.textContent += '\n\nSubmitting…';
              const submitGeneration = generation;
              const payload={pr_number:pr,context_id:draft.context_id,decision_id:draft.decision_id,head_sha:draft.head_sha,contract_digest:draft.contract_digest,verdict:draft.verdict,directive:draft.directive,rationale:draft.rationale,supersedes:draft.tips};
              sessionStorage.setItem(attemptKey, draft.decision_id);
              try {
                const result=await request('/submit',{method:'POST',body:JSON.stringify(payload)});
                if (!connected || submitGeneration !== generation) return;
                output.textContent += `\nPublication: ${result.status}; decision ${result.decision_id}; ` +
                  `comment ${result.github_comment_id || 'not yet confirmed'}. This does not mean code fixed, ` +
                  `regression verified, PASS, or PR approved.`;
                if (result.status === 'published-awaiting-processing') sessionStorage.removeItem(attemptKey);
              }
              catch (exc) {
                if (!connected || submitGeneration !== generation) return;
                if (exc.status >= 400 && exc.status < 500 && exc.status !== 401) sessionStorage.removeItem(attemptKey);
                output.textContent += `\nSubmission response was not confirmed: ${exc.message}. Your rationale remains above. ` +
                  `Use publication-status recovery for this decision before preparing a fresh context and decision.`;
                confirm.disabled=false;
              }
            };
            card.append(select,rationale,preview,output,confirm);
          }
          byId('adj-login-button').onclick = async () => {
            const input=byId('adj-secret'); const secret=input.value; input.value=''; error('');
            try { const result=await request('/login',{method:'POST',body:JSON.stringify({secret})}); csrf=result.csrf_token; sessionStorage.setItem(`adj-csrf-${pr}`,csrf); await loadSession(); }
            catch (exc) { error(exc.message); }
          };
          byId('adj-logout').onclick = async () => { await request('/logout',{method:'POST'}).catch(()=>{}); csrf=''; sessionStorage.removeItem(`adj-csrf-${pr}`); byId('adj-session').hidden=true; byId('adj-login').hidden=false; };
          byId('adj-refresh').onclick = refresh;
          boot();
        })();
        """.replace(
            "__PR__", json.dumps(pr_number)
        ).replace(
            "__REPO__", json.dumps(repo_name)
        )
        # Queue the controller after NiceGUI has created the page elements;
        # an inline body script can run before ``adj-root`` exists.
        ui.run_javascript(script)
