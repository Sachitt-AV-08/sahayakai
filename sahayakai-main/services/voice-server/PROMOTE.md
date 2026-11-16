# Promoting a voice-server candidate to production

The live parent-call engine is Cloud Run service `sahayakai-telephony`
(project `sahayakai-b4248`, region `asia-southeast1`). `cloudbuild.yaml` is its
only deploy definition. It always deploys a **no-traffic candidate** tagged
`candidate`, so nothing reaches a parent until someone promotes it by hand,
in this order.

## 1. Before building: the order is not optional

1. **Know what a stop request does.** This engine never blocks a number: it
   writes nothing to `call_suppressions`. When a parent may have asked not to
   be called again it records `optOutRequested: {quote, at, source}` on the
   outreach (`source` is `live`, heard on the call, or `analysis`, found
   after it), leads the teacher's call summary with a note quoting the
   parent, says "Thank you, I will let the class teacher know you would
   prefer not to receive these calls" in the call language and closes the
   call. Whether that parent is called again is the teacher's decision. The
   non-English lines in `src/call_lines.json` marked `unsure` need a native
   speaker to read them aloud before they are trusted on a live call.
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
- [ ] "Don't call me now, I'm driving" does **not** end the call, and the
      outreach has no `optOutRequested`.
- [ ] On a second test call: "please don't call me again" gets the fixed
      "I will let the class teacher know" line and, if you then say nothing,
      the leg hangs up within about 15 seconds. The outreach shows
      `optOutRequested` with your words and `source: live`, the summary's
      first action item quotes you, and **no** `call_suppressions` document
      was created.
- [ ] A normal ending (goodbye) hangs up by itself within a few seconds.
- [ ] Cloud Logging for the candidate revision has no DEBUG lines, no
      transcript text, no phone numbers and no `?t=` token values.

Remove `optOutRequested` from the test outreach afterwards.

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

Rolling back to 00037 brings back its behaviour: it has no stop handling at
all, so a parent's "please don't call me again" is answered by the model and
is not recorded for the teacher.
