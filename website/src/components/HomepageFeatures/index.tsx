import type {ReactNode} from 'react';
import clsx from 'clsx';
import Heading from '@theme/Heading';
import styles from './styles.module.css';

type FeatureItem = {
  title: string;
  emoji: string;
  description: ReactNode;
};

const FeatureList: FeatureItem[] = [
  {
    title: 'Send & Receive',
    emoji: '\u{1F4E7}',
    description: (
      <>
        Send messages to Signal users and groups. Receive incoming messages with
        full emoji reaction support — including Note to Self sync reactions.
      </>
    ),
  },
  {
    title: 'Claude Channel',
    emoji: '\u{1F4F1}',
    description: (
      <>
        Push incoming messages to Claude Code in real time via channel
        notifications. No polling required — Claude sees messages the moment
        they arrive.
      </>
    ),
  },
  {
    title: 'Fast JSON-RPC',
    emoji: '⚡',
    description: (
      <>
        Talks to a persistent signal-cli daemon over TCP. No JVM cold start per
        request — calls are instant, and concurrent callers share one daemon.
      </>
    ),
  },
];

function Feature({title, emoji, description}: FeatureItem) {
  return (
    <div className="col col--4">
      <div className={styles.featureCard}>
        <div className={styles.featureIcon} aria-hidden="true">
          {emoji}
        </div>
        <Heading as="h3" className={styles.featureTitle}>
          {title}
        </Heading>
        <p className={styles.featureDesc}>{description}</p>
      </div>
    </div>
  );
}

export default function HomepageFeatures(): ReactNode {
  return (
    <section className={styles.features}>
      <div className="container">
        <div className={styles.sectionHead}>
          <span className={styles.sectionEyebrow}>Why Signal MCP?</span>
          <Heading as="h2" className={styles.sectionTitle}>
            A private, powerful bridge between agents and people
          </Heading>
        </div>
        <div className={clsx('row', styles.featureRow)}>
          {FeatureList.map((props, idx) => (
            <Feature key={idx} {...props} />
          ))}
        </div>
      </div>
    </section>
  );
}
