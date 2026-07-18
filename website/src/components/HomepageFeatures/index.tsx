import type {ReactNode} from 'react';
import Heading from '@theme/Heading';
import styles from './styles.module.css';

type IconProps = {children: ReactNode};

function Icon({children}: IconProps) {
  return (
    <svg
      width="26"
      height="26"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round">
      {children}
    </svg>
  );
}

const icons: Record<string, ReactNode> = {
  send: (
    <Icon>
      <path d="M22 2 11 13M22 2 15 22l-4-9-9-4 20-7z" />
    </Icon>
  ),
  react: (
    <Icon>
      <circle cx="12" cy="12" r="10" />
      <path d="M8 14s1.5 2 4 2 4-2 4-2" />
      <line x1="9" y1="9" x2="9.01" y2="9" />
      <line x1="15" y1="9" x2="15.01" y2="9" />
    </Icon>
  ),
  receive: (
    <Icon>
      <path d="M22 12h-6l-2 3h-4l-2-3H2" />
      <path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z" />
    </Icon>
  ),
  channel: (
    <Icon>
      <path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z" />
    </Icon>
  ),
  shield: (
    <Icon>
      <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
    </Icon>
  ),
  server: (
    <Icon>
      <rect x="2" y="3" width="20" height="8" rx="2" />
      <rect x="2" y="13" width="20" height="8" rx="2" />
      <line x1="6" y1="7" x2="6.01" y2="7" />
      <line x1="6" y1="17" x2="6.01" y2="17" />
    </Icon>
  ),
};

type FeatureItem = {
  title: string;
  icon: keyof typeof icons;
  description: string;
};

const FeatureList: FeatureItem[] = [
  {
    title: 'Send anywhere',
    icon: 'send',
    description:
      'Message Signal users and groups by phone number, group id, or display name.',
  },
  {
    title: 'Emoji reactions',
    icon: 'react',
    description:
      'React to any message — and remove reactions — just like a human on Signal.',
  },
  {
    title: 'Receive & parse',
    icon: 'receive',
    description:
      "Parse incoming text, reactions, and Note-to-Self syncs plain text can't recover.",
  },
  {
    title: 'Claude Channel mode',
    icon: 'channel',
    description:
      'Push messages to Claude in real time. No polling loop — your agent sees everything instantly.',
  },
  {
    title: 'Trusted recipients',
    icon: 'shield',
    description:
      "Enforce an allowlist so your agent can only message people you've approved.",
  },
  {
    title: 'Persistent daemon',
    icon: 'server',
    description:
      'Talks to a warm signal-cli daemon — instant calls, no 2–3s JVM cold start.',
  },
];

function Feature({title, icon, description}: FeatureItem) {
  return (
    <div className="sig-card sig-card--hover">
      <div className="sig-card__icon">{icons[icon]}</div>
      <Heading as="h3" className="sig-card__title">
        {title}
      </Heading>
      <p className="sig-card__body">{description}</p>
    </div>
  );
}

export default function HomepageFeatures(): ReactNode {
  return (
    <section className={styles.features}>
      <div className={styles.featuresHeader}>
        <span className="sig-eyebrow">Everything in one server</span>
        <Heading as="h2" className={styles.featuresTitle}>
          Built for agents that talk back.
        </Heading>
      </div>
      <div className={styles.featuresGrid}>
        {FeatureList.map((props) => (
          <Feature key={props.title} {...props} />
        ))}
      </div>
    </section>
  );
}
