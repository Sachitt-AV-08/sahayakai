# Promoting a voice-server candidate to production

The live parent-call engine is Cloud Run service `sahayakai-telephony`
(project `sahayakai-b4248`, region `asia-southeast1`). `cloudbuild.yaml` is its
only deploy definition. It always deploys a **no-traffic candidate** tagged
`candidate`, so nothing reaches a parent until someone promotes it by hand,
in this order.

## 1. Before building: the order is not optional

1. **The web reader is live first.** This engine tells a parent who confirms a
   stop "we will not call you again" and writes `call_suppressions/{sha256(E.164)}`.
   That is only true once the web app's dial routes refuse a suppressed number
   (`refuseIfParentOptedOut`, PR #193). Deploy #193 to production **in both
   regions** and check that a suppressed test number gets `409 PARENT_OPTED_OUT`
   before this engine takes any traffic.
2. **The dependency lock exists.** The image installs only from
   `requirements.lock`. Generate it with `scripts/lock.sh` (needs Docker and
   PyPI access), review the diff and commit it. Without the lock the image
   build fails on purpose.
3. **Choose the Gemini backend.** On 2026-11-16 the AI Studio key returned
   `402 RESOURCE_EXHAUSTED`. To run on Vertex AI as the service account, add
   `SAHAYAKAI_GEMINI_BACKEND=vertex` (and optionally
   `SAHAYAKAI_VERTEX_LOCATION`, default `asia-south1`) to `--set-env-vars` in
   `cloudbuild.yaml`, as its own reviewed change.
4. **Optional but recommended:** a Firestore TTL policy on
   `vobiz_stream_tokens.expiresAt`, so consumed-stream records are swept.

## 2. Build the candidate

From the monorepo root:

```sh
gcloud builds submit . \
  --config=sahayakai-main/services/voice-server/cloudbuild.yaml \
  --ignore-file=sahayakai-main/services/voice-server/cloudbuild.gcloudignore \
  --project=sahayakai-b4248
```

The build runs the unit and contract gates, builds the image, runs
`src/smoke_check.py` inside it against the real pipecat, then pushes and deploys
`--no-traffic --tag=candidate`. Any red step stops it.

Confirm that production is untouched and the candidate is ready:

```sh
gcloud run services describe sahayakai-telephony --region=asia-southeast1 \
  --project=sahayakai-b4248 --format='yaml(status.traffic)'
curl -fsS https://candidate---sahayakai-telephony-zwydpvyuca-as.a.run.app/healthz
```

Note the revision that currently has 100% of traffic. That is the rollback target.

## 3. Test call through the candidate (never a real parent)

Route one call from a test outreach, to a team member's own number, to the
candidate URL. How the web app points a single call at the candidate tag is
the founder's decision to make before this step.

On that call, check:

- [ ] A concern-reason outreach opens with the fixed request-to-talk line, and
      nothing about the concern is said. Asking "why?" gets the
      "explain in person" line.
- [ ] "Don't call me now, I'm driving" does **not** end the call, and no
      `call_suppressions` document appears.
- [ ] "Please don't call me again", then "no", carries on, and nothing is
      written.
- [ ] On a second test call: "please don't call me again", then "yes", gives
      the acknowledgement and the leg hangs up. `call_suppressions/{sha256}`
      exists, the outreach shows `optedOut`, and the transcript and summary show
      the request. The next dial to that number is refused with `409`.
- [ ] A normal ending (goodbye) hangs up by itself within a few seconds.
- [ ] Cloud Logging for the candidate revision has no DEBUG lines, no
      transcript text, no phone numbers and no `?t=` token values.

Delete the test suppression document afterwards, so the team member's number
can be called again.

## 4. Promote

```sh
gcloud run services update-traffic sahayakai-telephony \
  --to-tags=candidate=100 --region=asia-southeast1 --project=sahayakai-b4248
```

Watch the first real calls in the logs.

## 5. Roll back: one command

Send all traffic back to the revision noted in step 2 (for example the current
`sahayakai-telephony-00037-f5b`):

```sh
gcloud run services update-traffic sahayakai-telephony \
  --to-revisions=sahayakai-telephony-00037-f5b=100 \
  --region=asia-southeast1 --project=sahayakai-b4248
```

Rolling back the engine does not lift any suppression it wrote. Those stay
until removed by hand, by design.
