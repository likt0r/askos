// Fehler der Oberfläche selbst: eine unbekannte Adresse, eine Ansicht oder die Hülle, die stehen blieb (#213).
export default {
  fault: {
    notFound: {
      nav: 'Nicht gefunden',
      title: 'Diese Seite gibt es nicht',
      text: 'Der Control Room hat unter dieser Adresse keine Seite.',
      back: 'Zurück zu den VMs',
    },
    view: {
      title: 'In dieser Ansicht ist etwas schiefgegangen',
      text: 'Ein Fehler in der Oberfläche des Control Room hat diese Ansicht angehalten. Die anderen Seiten funktionieren weiter; lade die Seite neu, um diese wieder zu starten.',
    },
    shell: {
      title: 'Im Control Room ist etwas schiefgegangen',
      text: 'Ein Fehler in der Oberfläche des Control Room hat sie angehalten. Lade die Seite neu, um sie wieder zu starten.',
    },
    toast: 'In der Oberfläche des Control Room ist etwas schiefgegangen',
    reload: 'Neu laden',
    details: 'Details',
    copy: 'Kopieren',
    copied: 'Kopiert',
  },
}
