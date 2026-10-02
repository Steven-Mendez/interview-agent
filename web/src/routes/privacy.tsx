import { createFileRoute } from "@tanstack/react-router"

import { LegalBackLink, LegalSeeAlso } from "@/components/legal-page"
import {
  PageContainer,
  PageHeader,
  PageShell,
  Section,
} from "@/components/ui/page"
import { PRODUCT_NAME, pageHead } from "@/lib/head"

const CONTACT_EMAIL = "stevenampaiz@gmail.com"

// Public on purpose: no requireSession. Google's OAuth consent screen links
// here, so it must open for someone who has never signed in. Every claim
// below follows what the code does (the tables in interview/db.py, the
// retention purge, the voice pipeline in agent.py, the Sentry and metrics
// filters); change the text when that behavior changes.
export const Route = createFileRoute("/privacy")({
  head: () => pageHead("Privacy policy"),
  component: PrivacyPage,
})

function PrivacyPage() {
  return (
    <PageShell>
      <PageContainer variant="reading" className="flex flex-col gap-8">
        <PageHeader
          title="Privacy policy"
          description={
            <>
              What {PRODUCT_NAME} keeps when you use it, where it goes and for
              how long. Last updated{" "}
              <time dateTime="2026-10-02">October 2, 2026</time>.
            </>
          }
          actions={<LegalBackLink />}
        />
        <div className="flex flex-col gap-8 text-sm leading-6 text-pretty text-foreground [&_p]:max-w-prose [&_section_a]:text-primary [&_section_a]:underline [&_section_a]:underline-offset-4">
          <Section title="Who runs it">
            <p>
              {PRODUCT_NAME} is an independent project run by its owner. For any
              question about your data, write to{" "}
              <a href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a>.
            </p>
          </Section>
          <Section title="Your account">
            <p>
              You sign in with Google or GitHub through Neon Auth. The app
              stores your account id, the email address and name your sign-in
              provides, when the account was created and when it was last seen.
              It also keeps how many interviews you have started (for your
              interview allowance) and the interviewer settings you save. The
              app&apos;s administrators can see the list of accounts. Neon Auth
              also keeps its own record of your account and of each sign-in
              session, such as the browser and IP address it came from, in the
              same database.
            </p>
          </Section>
          <Section title="What you give the app">
            <p>
              To prepare an interview you upload your resume as a PDF and paste
              a job offer. The app extracts the resume&apos;s text and keeps
              that text and the file name; the PDF itself is not stored. It also
              stores the job offer, the interview plan made from them, the
              transcript of what you and the interviewer said, and the
              evaluation of your answers. All of it lives in a Postgres database
              hosted by Neon in the US East region.
            </p>
          </Section>
          <Section title="Voice and AI processing">
            <p>
              During the interview your audio travels through LiveKit Cloud.
              Your speech is transcribed, and the interviewer&apos;s voice
              synthesized, through LiveKit Inference and its speech providers
              (AssemblyAI, Cartesia, Inworld). The planner, the interviewer and
              the evaluator run on OpenAI models, which receive your resume
              text, the job offer and the transcript.
            </p>
            <p>
              The app&apos;s server runs on FastAPI Cloud and the interviewer on
              LiveKit Cloud; both handle your resume, the job offer and the
              transcript while they work on them, and the live transcript shown
              during the interview also passes through LiveKit Cloud. The
              website is served by Vercel.
            </p>
            <p>
              The app keeps no audio: it stores the transcript only, and it
              starts each session with LiveKit&apos;s recording turned off.
            </p>
          </Section>
          <Section title="How long it is kept">
            <p>
              Interviews (resume text, job offer, plan, transcript and
              evaluation) are deleted automatically 30 days after they were
              created. Your account profile, interview count and settings are
              kept until the account is removed.
            </p>
          </Section>
          <Section title="Monitoring">
            <p>
              The app records anonymous usage metrics: counts and durations,
              such as how long a reply took, with no interview content and no
              account or interview ids. Error reports go to Sentry with what
              locates the failure (the error type and where in the code it
              happened); they carry no interview content, no error messages and
              no request bodies, and identify your account only by its opaque
              id. No third-party tracing of interview content is enabled.
            </p>
          </Section>
          <Section title="Cookies and local storage">
            <p>
              Neon Auth keeps your session with cookies on its own domain, and
              the page holds a sign-in token in memory while it is open. The app
              remembers your light or dark theme in the browser&apos;s local
              storage. There are no advertising or analytics cookies.
            </p>
          </Section>
          <Section title="Your rights">
            <p>
              You can see your profile and your interviews in the app at any
              time. To have an interview, or your account and all its data,
              deleted sooner, email{" "}
              <a href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a>.
            </p>
          </Section>
          <Section title="Changes">
            <p>
              When this policy changes, this page is updated and the date at the
              top changes with it.
            </p>
          </Section>
        </div>
        <LegalSeeAlso to="/terms">Terms of service</LegalSeeAlso>
      </PageContainer>
    </PageShell>
  )
}
