import { Link, createFileRoute } from "@tanstack/react-router"

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
// here next to the privacy policy, so it must open for someone who has never
// signed in. The limits it describes are configuration (the interview quotas
// and the concurrency cap in config.py), so it names them without numbers;
// the app shows each non-admin account how many of its interviews are left.
// Users never see the word "guest", so the page says who the quotas cover
// instead. Only pricing gets a promise of advance notice: the limits are
// settings the operator can lower with a restart. Data handling lives in the
// privacy policy and is only referred to here.
export const Route = createFileRoute("/terms")({
  head: () => pageHead("Terms of service"),
  component: TermsPage,
})

function TermsPage() {
  return (
    <PageShell>
      <PageContainer variant="reading" className="flex flex-col gap-8">
        <PageHeader
          title="Terms of service"
          description={
            <>
              The rules for using {PRODUCT_NAME}, and what it does and does not
              promise. Last updated{" "}
              <time dateTime="2026-10-02">October 2, 2026</time>.
            </>
          }
          actions={<LegalBackLink />}
        />
        <div className="flex flex-col gap-8 text-sm leading-6 text-pretty text-foreground [&_p]:max-w-prose [&_section_a]:text-primary [&_section_a]:underline [&_section_a]:underline-offset-4">
          <Section title="What the service is">
            <p>
              {PRODUCT_NAME} is a practice tool: it simulates a job interview by
              voice with an AI interviewer, built from your resume and a job
              offer, and scores your answers when it ends. It is an independent
              project run by its owner. For any question about these terms,
              write to <a href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a>.
            </p>
          </Section>
          <Section title="No advice, no guarantee">
            <p>
              The interview plan, the questions and the evaluation are produced
              by AI models and may be wrong or incomplete. They are practice
              feedback, not career, legal or hiring advice, and a good score
              implies nothing about any outcome with any employer.
            </p>
            <p>
              The service is provided as is. It may be unavailable, changed or
              shut down at any time, and an interview may be cut short by its
              time limits or by a failure along the way.
            </p>
          </Section>
          <Section title="Your account and limits">
            <p>
              You sign in with Google or GitHub, and each person uses one
              account. Every account other than the operator&apos;s own
              administrators gets a fixed number of interviews in total, and
              those accounts share a monthly allowance; both are limits the
              operator sets, and the app shows how many of yours you have left.
              Only a few interviews can run at the same time, so a new session
              may be refused while the service is busy.
            </p>
          </Section>
          <Section title="Acceptable use">
            <p>
              Use only your own resume, and job offers, real or made up for
              practice, that you are allowed to use. Do not upload other
              people&apos;s personal data. Do not try to break, overload, scrape
              or reverse engineer the service, get around its limits, or use it
              for anything unlawful. The operator may suspend or remove accounts
              that do.
            </p>
          </Section>
          <Section title="Your content">
            <p>
              You keep the rights to your resume, your answers and the job
              offers you paste. You allow the service to process them, with the
              providers named in the <Link to="/privacy">privacy policy</Link>,
              only to run and evaluate your interviews. They are kept for the
              period the privacy policy states, and you can ask for them to be
              deleted sooner by writing to{" "}
              <a href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a>.
            </p>
          </Section>
          <Section title="Cost">
            <p>
              The service is free of charge today. The operator may add pricing
              later, with notice on this page first, and may change the limits
              above at any time.
            </p>
          </Section>
          <Section title="Changes">
            <p>
              When these terms change, this page is updated and the date at the
              top changes with it.
            </p>
          </Section>
        </div>
        <LegalSeeAlso to="/privacy">Privacy policy</LegalSeeAlso>
      </PageContainer>
    </PageShell>
  )
}
