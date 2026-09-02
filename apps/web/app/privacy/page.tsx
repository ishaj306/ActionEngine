import Link from "next/link";
import styles from "./page.module.css";

/*
 * Written to be read, not to be defensible.
 *
 * People hand this system marksheets, income certificates and identity
 * documents. A page of hedged clauses is worse than nothing here: it tells a
 * reader that the answer is being kept from them. Every statement below is one
 * a specific line of code makes true, and the two things the system does badly
 * are stated as plainly as the things it does well.
 */

export const metadata = {
  title: "What happens to your documents",
};

export default function Privacy() {
  return (
    <main className={styles.page}>
      <p className={styles.back}>
        <Link href="/">← Back</Link>
      </p>

      <h1 className={styles.title}>What happens to your documents</h1>
      <p className={styles.lede}>
        You are uploading things like marksheets and income certificates. Here
        is exactly what this does with them, in the order it does it.
      </p>

      <section className={styles.section}>
        <h2>The file itself is not kept</h2>
        <p>
          The text is extracted from your upload and the file is discarded. It
          is never written to disk and never stored in the database. What is
          kept is the extracted text, the page boundaries needed to highlight a
          sentence, and whatever you tick off in the plan.
        </p>
        <p className={styles.aside}>
          This is not a nicety. A stored PDF of somebody&rsquo;s Aadhaar buys
          nothing once the text is out, and removes an entire category of
          breach by not existing.
        </p>
      </section>

      <section className={styles.section}>
        <h2>Only you can read yours</h2>
        <p>
          Every document is stored against your account, and every read is
          filtered by it inside the storage layer rather than at each endpoint
          — so a route cannot forget to check. Asking for a document belonging
          to somebody else returns the same &ldquo;not found&rdquo; as asking
          for one that never existed.
        </p>
      </section>

      <section className={styles.section}>
        <h2>What leaves this server</h2>
        <p>
          By default, <strong>nothing</strong>. All extraction — dates,
          requirements, dependencies, scheduling, eligibility — runs locally as
          ordinary code. There is no third-party analytics, no error tracker,
          and no telemetry.
        </p>
        <p>
          If the operator has switched on the model arm, the text of your
          document is sent to Anthropic&rsquo;s API to be read, and the answer
          is checked against your document before any of it is shown to you.
          The status endpoint reports which mode is running, so you can check
          rather than take this on trust. Your name, email and account id are
          never included.
        </p>
      </section>

      <section className={styles.section}>
        <h2>What gets logged</h2>
        <p>
          Filenames, counts and timings. Never document text — not an extracted
          deadline, not a requirement, not a sentence. A log line is the easiest
          place for content to end up somewhere nobody intended, so the rule is
          absolute rather than case-by-case.
        </p>
      </section>

      <section className={styles.section}>
        <h2>Deleting means deleting</h2>
        <p>
          Deleting a document removes the row. Deleting everything removes every
          row you own. Neither marks anything as hidden and leaves it there, and
          there is no separate copy to go stale.
        </p>
      </section>

      <section className={styles.section}>
        <h2>Two things this does not do well</h2>
        <ul className={styles.plain}>
          <li>
            <strong>There is no retention limit.</strong> Your documents stay
            until you delete them. Nothing expires them on a schedule, so
            deleting what you no longer need is worth doing.
          </li>
          <li>
            <strong>Sign-in is handled by Clerk,</strong> a third party, which
            therefore knows your email address and when you signed in. It never
            sees a document.
          </li>
        </ul>
      </section>

      <p className={styles.footnote}>
        Every statement here is one a specific piece of the code makes true. If
        one of them stops being true, the code changed and this page should have
        changed with it.
      </p>
    </main>
  );
}
