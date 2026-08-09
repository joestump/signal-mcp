import type {ReactNode} from 'react';
import clsx from 'clsx';
import Link from '@docusaurus/Link';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import Layout from '@theme/Layout';
import HomepageFeatures from '@site/src/components/HomepageFeatures';
import Heading from '@theme/Heading';

import styles from './index.module.css';

function ChatMock(): ReactNode {
  return (
    <div className={styles.chatMock} aria-hidden="true">
      <div className={styles.chatHeader}>
        <span className={styles.chatAvatar}>🤖</span>
        <div>
          <div className={styles.chatName}>Claude</div>
          <div className={styles.chatStatus}>via Signal MCP</div>
        </div>
      </div>
      <div className={styles.chatBody}>
        <div className={clsx(styles.bubble, styles.bubbleIn)}>
          Ping me on Signal when the deploy finishes.
        </div>
        <div className={clsx(styles.bubble, styles.bubbleOut)}>
          Deploy to prod succeeded ✅ 4m12s
        </div>
        <div className={clsx(styles.bubble, styles.bubbleOut)}>
          All 218 tests green. Want the changelog?
        </div>
        <div className={clsx(styles.bubble, styles.bubbleIn)}>👍</div>
      </div>
      <div className={styles.chatInput}>
        <span>Signal message</span>
        <span className={styles.chatSend}>↑</span>
      </div>
    </div>
  );
}

function HomepageHeader() {
  const {siteConfig} = useDocusaurusContext();
  return (
    <header className={styles.hero}>
      <div className={clsx('container', styles.heroInner)}>
        <div className={styles.heroCopy}>
          <span className={styles.eyebrow}>Model Context Protocol</span>
          <Heading as="h1" className={styles.heroTitle}>
            Speak freely,<br />
            from any agent.
          </Heading>
          <p className={styles.heroSubtitle}>{siteConfig.tagline} — end‑to‑end
            encrypted, over a persistent <code>signal-cli</code> daemon.</p>
          <div className={styles.heroButtons}>
            <Link className="button button--secondary button--lg" to="/docs/intro">
              Get Started
            </Link>
            <Link
              className="button button--outline button--primary button--lg"
              to="https://github.com/joestump/signal-mcp">
              View on GitHub
            </Link>
          </div>
        </div>
        <div className={styles.heroArt}>
          <ChatMock />
        </div>
      </div>
    </header>
  );
}

export default function Home(): ReactNode {
  const {siteConfig} = useDocusaurusContext();
  return (
    <Layout
      title="Signal for AI agents"
      description="Send and receive Signal messages from any AI agent via the Model Context Protocol">
      <HomepageHeader />
      <main>
        <HomepageFeatures />
      </main>
    </Layout>
  );
}
