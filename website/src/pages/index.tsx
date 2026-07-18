import type {ReactNode} from 'react';
import clsx from 'clsx';
import Link from '@docusaurus/Link';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import Layout from '@theme/Layout';
import HomepageFeatures from '@site/src/components/HomepageFeatures';
import Heading from '@theme/Heading';

import styles from './index.module.css';

function HeroThread() {
  return (
    <div className={clsx('sig-thread', styles.thread)}>
      <div className={styles.threadHeader}>
        <span className="sig-avatar sig-avatar--note">✳︎</span>
        <div>
          <div className={styles.threadName}>Note to Self</div>
          <div className={styles.threadVia}>via Claude agent</div>
        </div>
      </div>
      <div className="sig-bubble sig-bubble--sent">
        cc deploy the release branch to staging
      </div>
      <div className="sig-bubble sig-bubble--received">
        On it — running the pipeline now. I'll ping you when it's green. ✅
      </div>
      <div className="sig-bubble sig-bubble--received">
        Deploy succeeded. 3 services updated, 0 errors.
        <span className="sig-bubble__meta">delivered · read</span>
      </div>
    </div>
  );
}

function HomepageHero() {
  return (
    <header className={styles.hero}>
      <div className={styles.heroInner}>
        <div className={styles.heroCopy}>
          <span className="sig-eyebrow">Model Context Protocol</span>
          <Heading as="h1" className="sig-display">
            Give your AI agents a Signal.
          </Heading>
          <p className={styles.heroSubtitle}>
            An MCP server for <code className="sig-code">signal-cli</code> that
            lets AI agents send, receive, and react to end-to-end encrypted
            Signal messages — in real time.
          </p>
          <div className={styles.heroButtons}>
            <Link className="sig-btn sig-btn--primary sig-btn--lg" to="/docs/intro">
              Get Started
            </Link>
            <Link
              className="sig-btn sig-btn--outline sig-btn--lg"
              href="https://github.com/joestump/signal-mcp">
              View on GitHub
            </Link>
          </div>
          <div className={styles.heroBadges}>
            <span className="sig-badge sig-badge--wash">Python 3.13+</span>
            <span className="sig-badge sig-badge--wash">signal-cli daemon</span>
            <span className="sig-badge sig-badge--wash">Claude Channel mode</span>
            <span className="sig-badge sig-badge--neutral">MIT</span>
          </div>
        </div>
        <HeroThread />
      </div>
    </header>
  );
}

function CtaBand() {
  return (
    <section className={styles.cta}>
      <div className={styles.ctaInner}>
        <div>
          <Heading as="h2" className={styles.ctaTitle}>
            Ship it in five minutes.
          </Heading>
          <p className={styles.ctaBody}>
            Start the daemon, run the server, wire it into Claude. That's it.
          </p>
        </div>
        <div className={styles.ctaButtons}>
          <Link className="sig-btn sig-btn--secondary sig-btn--lg" to="/docs/intro">
            Read the docs
          </Link>
          <Link className="sig-btn sig-btn--inverse sig-btn--lg" to="/docs/tools">
            API reference
          </Link>
        </div>
      </div>
    </section>
  );
}

export default function Home(): ReactNode {
  const {siteConfig} = useDocusaurusContext();
  return (
    <Layout
      title={siteConfig.title}
      description="An MCP server for signal-cli that lets AI agents send, receive, and react to end-to-end encrypted Signal messages — in real time.">
      <HomepageHero />
      <main>
        <HomepageFeatures />
        <CtaBand />
      </main>
    </Layout>
  );
}
