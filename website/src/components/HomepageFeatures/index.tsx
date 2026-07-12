import type {ReactNode} from 'react';
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
    emoji: '\u26A1',
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
      <div className="text--center" style={{fontSize: '3rem', marginBottom: '1rem'}}>
        {emoji}
      </div>
      <div className="text--center padding-horiz--md">
        <Heading as="h3">{title}</Heading>
        <p>{description}</p>
      </div>
    </div>
  );
}

export default function HomepageFeatures(): ReactNode {
  return (
    <section className={styles.features}>
      <div className="container">
        <div className="row">
          {FeatureList.map((props, idx) => (
            <Feature key={idx} {...props} />
          ))}
        </div>
      </div>
    </section>
  );
}
