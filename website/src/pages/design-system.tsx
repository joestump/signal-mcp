import type {ReactNode} from 'react';
import Layout from '@theme/Layout';
import Heading from '@theme/Heading';
import styles from './design-system.module.css';

type Swatch = {
  name: string;
  varName: string;
  hex: string;
  note?: string;
  onDark?: boolean;
};

const brand: Swatch[] = [
  {name: 'Ultramarine', varName: '--sig-ultramarine', hex: '#3b45fd', note: 'Primary brand'},
  {name: 'Ultramarine Hover', varName: '--sig-ultramarine-hover', hex: '#2b34d6', note: 'Pressed / hover'},
  {name: 'Wash', varName: '--sig-wash', hex: '#e3e8fe', note: 'Soft brand tint'},
  {name: 'Periwinkle', varName: '--sig-periwinkle', hex: '#7c96f5', note: 'Decorative'},
  {name: 'Chat Blue', varName: '--sig-bubble-sent', hex: '#2f6bed', note: 'Sent bubble'},
  {name: 'Note Lavender', varName: '--sig-note-lavender', hex: '#cabcf6', note: 'Note to Self'},
];

const neutrals: Swatch[] = [
  {name: 'Ink', varName: '--sig-ink', hex: '#17171b', onDark: true},
  {name: 'Gray 800', varName: '--sig-gray-800', hex: '#2c2c30', onDark: true},
  {name: 'Gray 500', varName: '--sig-gray-500', hex: '#6b6b73', onDark: true},
  {name: 'Gray 300', varName: '--sig-gray-300', hex: '#c9c9d1'},
  {name: 'Gray 100', varName: '--sig-gray-100', hex: '#f2f2f5'},
  {name: 'White', varName: '--sig-white', hex: '#ffffff'},
];

const semantic: Swatch[] = [
  {name: 'Success', varName: '--sig-success', hex: '#2c9c5a', onDark: true},
  {name: 'Warning', varName: '--sig-warning', hex: '#e6a817'},
  {name: 'Danger', varName: '--sig-danger', hex: '#f5432c', onDark: true, note: 'End-call red'},
];

function SwatchGrid({items}: {items: Swatch[]}) {
  return (
    <div className={styles.swatchGrid}>
      {items.map((s) => (
        <div key={s.varName} className={styles.swatch}>
          <div
            className={styles.swatchChip}
            style={{
              background: s.hex,
              color: s.onDark ? '#fff' : '#17171b',
              borderColor: s.hex.toLowerCase() === '#ffffff' ? 'var(--sig-gray-200)' : 'transparent',
            }}>
            <span>{s.hex}</span>
          </div>
          <div className={styles.swatchMeta}>
            <strong>{s.name}</strong>
            <code>{s.varName}</code>
            {s.note && <span className={styles.swatchNote}>{s.note}</span>}
          </div>
        </div>
      ))}
    </div>
  );
}

const typeScale = [
  {label: 'Display', size: '4rem', weight: 800, sample: 'Speak freely'},
  {label: 'H1', size: '2.5rem', weight: 800, sample: 'Why Signal MCP?'},
  {label: 'H2', size: '1.9rem', weight: 800, sample: 'End-to-end encrypted'},
  {label: 'H3', size: '1.35rem', weight: 700, sample: 'Send & receive'},
  {label: 'Body', size: '1.05rem', weight: 400, sample: 'Say hello to a different messaging experience.'},
  {label: 'Small', size: '0.85rem', weight: 500, sample: 'via Signal MCP'},
];

const radii = [
  {name: 'xs', v: '--sig-radius-xs', px: 6},
  {name: 'sm', v: '--sig-radius-sm', px: 10},
  {name: 'md', v: '--sig-radius-md', px: 14},
  {name: 'lg', v: '--sig-radius-lg', px: 20},
  {name: 'xl', v: '--sig-radius-xl', px: 28},
  {name: 'pill', v: '--sig-radius-pill', px: 999},
];

const spacing = [
  {n: '1', px: 4},
  {n: '2', px: 8},
  {n: '3', px: 12},
  {n: '4', px: 16},
  {n: '5', px: 24},
  {n: '6', px: 32},
  {n: '7', px: 48},
  {n: '8', px: 64},
];

function Section({
  id,
  eyebrow,
  title,
  children,
}: {
  id: string;
  eyebrow: string;
  title: string;
  children: ReactNode;
}) {
  return (
    <section className={styles.section} id={id}>
      <span className={styles.eyebrow}>{eyebrow}</span>
      <Heading as="h2" className={styles.sectionTitle}>
        {title}
      </Heading>
      {children}
    </section>
  );
}

export default function DesignSystem(): ReactNode {
  return (
    <Layout
      title="Design System"
      description="The Signal-inspired design language behind the Signal MCP site.">
      <header className={styles.dsHero}>
        <div className="container">
          <span className={styles.eyebrow}>Design Language</span>
          <Heading as="h1" className={styles.dsTitle}>
            The Signal design system
          </Heading>
          <p className={styles.dsLead}>
            A privacy-first, high-contrast visual language built on Signal's
            brand kit — Ultramarine, generous whitespace, Inter type, and fully
            rounded pills. These are the exact tokens powering this site and the
            published claude.ai/design library.
          </p>
        </div>
      </header>

      <main className={`container ${styles.dsMain}`}>
        <Section id="colors" eyebrow="Foundations" title="Color">
          <h3 className={styles.subhead}>Brand</h3>
          <SwatchGrid items={brand} />
          <h3 className={styles.subhead}>Neutrals</h3>
          <SwatchGrid items={neutrals} />
          <h3 className={styles.subhead}>Semantic</h3>
          <SwatchGrid items={semantic} />
        </Section>

        <Section id="type" eyebrow="Foundations" title="Typography">
          <p className={styles.para}>
            <strong>Inter</strong> across the board — tight tracking on
            headings, comfortable body leading. Headings run heavy (800) to echo
            Signal's bold marketing voice.
          </p>
          <div className={styles.typeStack}>
            {typeScale.map((t) => (
              <div key={t.label} className={styles.typeRow}>
                <span className={styles.typeLabel}>
                  {t.label}
                  <code>
                    {t.size} / {t.weight}
                  </code>
                </span>
                <span
                  className={styles.typeSample}
                  style={{fontSize: t.size, fontWeight: t.weight}}>
                  {t.sample}
                </span>
              </div>
            ))}
          </div>
        </Section>

        <Section id="radii" eyebrow="Foundations" title="Radius & Spacing">
          <h3 className={styles.subhead}>Corner radius</h3>
          <div className={styles.radiiGrid}>
            {radii.map((r) => (
              <div key={r.name} className={styles.radiiItem}>
                <div
                  className={styles.radiiBox}
                  style={{borderRadius: r.px === 999 ? '999px' : `${r.px}px`}}
                />
                <strong>{r.name}</strong>
                <code>{r.px === 999 ? 'pill' : `${r.px}px`}</code>
              </div>
            ))}
          </div>
          <h3 className={styles.subhead}>Spacing scale (4px base)</h3>
          <div className={styles.spaceStack}>
            {spacing.map((s) => (
              <div key={s.n} className={styles.spaceRow}>
                <code className={styles.spaceLabel}>space-{s.n}</code>
                <div className={styles.spaceBar} style={{width: `${s.px}px`}} />
                <span className={styles.spacePx}>{s.px}px</span>
              </div>
            ))}
          </div>
        </Section>

        <Section id="buttons" eyebrow="Components" title="Buttons">
          <p className={styles.para}>
            Every button is a full pill. Primary is Ultramarine on a soft brand
            shadow; secondary is the white-on-brand “Get Signal” style; outline
            for tertiary actions.
          </p>
          <div className={styles.componentRow}>
            <button className={`button button--primary button--lg`}>Get Started</button>
            <button className={`button button--secondary button--lg`}>Get Signal</button>
            <button className={`button button--outline button--primary button--lg`}>
              Learn more
            </button>
          </div>
          <div className={styles.componentRow}>
            <button className={`button button--primary`}>Default</button>
            <button className={`button button--primary button--sm`}>Small</button>
          </div>
        </Section>

        <Section id="badges" eyebrow="Components" title="Badges & Chips">
          <div className={styles.componentRow}>
            <span className={`${styles.badge} ${styles.badgeBrand}`}>Encrypted</span>
            <span className={`${styles.badge} ${styles.badgeWash}`}>Note to Self</span>
            <span className={`${styles.badge} ${styles.badgeSuccess}`}>Delivered</span>
            <span className={`${styles.badge} ${styles.badgeDanger}`}>Failed</span>
            <span className={`${styles.badge} ${styles.badgeNeutral}`}>Draft</span>
          </div>
        </Section>

        <Section id="cards" eyebrow="Components" title="Cards">
          <div className={styles.cardDemoGrid}>
            <div className={styles.demoCard}>
              <div className={styles.demoCardIcon}>🔒</div>
              <strong>Insecurity? Never.</strong>
              <p>
                State-of-the-art end-to-end encryption powered by the open source
                Signal Protocol.
              </p>
            </div>
            <div className={styles.demoCard}>
              <div className={styles.demoCardIcon}>⚡</div>
              <strong>Fast JSON-RPC</strong>
              <p>Persistent daemon over TCP — no JVM cold start per request.</p>
            </div>
          </div>
        </Section>

        <Section id="bubbles" eyebrow="Components" title="Chat bubbles">
          <p className={styles.para}>
            The signature Signal element. Sent bubbles use the in-app chat blue;
            received bubbles are neutral. Both tuck one corner in tight.
          </p>
          <div className={styles.bubbleDemo}>
            <div className={`${styles.bubble} ${styles.bubbleIn}`}>
              Ping me when the deploy finishes.
            </div>
            <div className={`${styles.bubble} ${styles.bubbleOut}`}>
              Deploy to prod succeeded ✅
            </div>
            <div className={`${styles.bubble} ${styles.bubbleOut}`}>
              All 218 tests green.
            </div>
            <div className={`${styles.bubble} ${styles.bubbleIn}`}>👍</div>
          </div>
        </Section>
      </main>
    </Layout>
  );
}
