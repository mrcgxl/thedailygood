"""Nachrichtenquellen für The Daily Good: Name, RSS-Adresse, Sprache.

Gemischt aus konstruktiven Quellen und großen Redaktionen. Die großen liefern viel
Schlechtes mit, das filtert die Redaktions-KI heraus.
"""

FEEDS = [
    # Konstruktiv und positiv
    ("Good News Network", "https://www.goodnewsnetwork.org/feed/", "Englisch"),
    ("Positive News", "https://www.positive.news/feed/", "Englisch"),
    ("Reasons to be Cheerful", "https://reasonstobecheerful.world/feed/", "Englisch"),
    ("Optimist Daily", "https://www.optimistdaily.com/feed/", "Englisch"),
    # Allgemein
    ("tagesschau", "https://www.tagesschau.de/index~rss2.xml", "Deutsch"),
    ("tagesschau Wirtschaft", "https://www.tagesschau.de/wirtschaft/index~rss2.xml", "Deutsch"),
    ("DW", "https://rss.dw.com/xml/rss-de-all", "Deutsch"),
    ("NPR", "https://feeds.npr.org/1001/rss.xml", "Englisch"),
    # Wissenschaft, Weltall, Gesundheit, Natur
    ("SPIEGEL Wissenschaft", "https://www.spiegel.de/wissenschaft/index.rss", "Deutsch"),
    ("Spektrum", "https://www.spektrum.de/alias/rss/spektrum-de-rss-feed/996406", "Deutsch"),
    ("scinexx", "https://www.scinexx.de/feed/", "Deutsch"),
    ("BBC Science", "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml", "Englisch"),
    ("Guardian Science", "https://www.theguardian.com/science/rss", "Englisch"),
    ("Guardian Environment", "https://www.theguardian.com/environment/rss", "Englisch"),
    ("NPR Science", "https://feeds.npr.org/1007/rss.xml", "Englisch"),
    ("NASA", "https://www.nasa.gov/news-release/feed/", "Englisch"),
    ("ESA", "https://www.esa.int/rssfeed/Our_Activities/Space_Science", "Englisch"),
    ("ScienceDaily", "https://www.sciencedaily.com/rss/top/science.xml", "Englisch"),
    ("ScienceDaily Health", "https://www.sciencedaily.com/rss/health_medicine.xml", "Englisch"),
    ("Phys.org", "https://phys.org/rss-feed/", "Englisch"),
    ("Mongabay", "https://news.mongabay.com/feed/", "Englisch"),
    ("Ärzteblatt", "https://www.aerzteblatt.de/rss/news.asp", "Deutsch"),
    # Technik
    ("heise online", "https://www.heise.de/rss/heise-atom.xml", "Deutsch"),
    ("Golem", "https://rss.golem.de/rss.php?feed=RSS2.0", "Deutsch"),
    ("t3n", "https://t3n.de/rss.xml", "Deutsch"),
    ("BBC Technology", "https://feeds.bbci.co.uk/news/technology/rss.xml", "Englisch"),
    ("The Verge", "https://www.theverge.com/rss/index.xml", "Englisch"),
    ("9to5Mac", "https://9to5mac.com/feed/", "Englisch"),
    # Gaming
    ("GameStar", "https://www.gamestar.de/news/rss/news.rss", "Deutsch"),
    ("GamePro", "https://www.gamepro.de/rss/gamepro.rss", "Deutsch"),
    ("Eurogamer", "https://www.eurogamer.net/feed", "Englisch"),
    ("Polygon", "https://www.polygon.com/rss/index.xml", "Englisch"),
    # Design, Grafik, Architektur, Wohnen
    ("Dezeen", "https://www.dezeen.com/feed/", "Englisch"),
    ("designboom", "https://www.designboom.com/feed/", "Englisch"),
    ("ArchDaily", "https://feeds.feedburner.com/Archdaily", "Englisch"),
    ("Creative Boom", "https://www.creativeboom.com/feed/", "Englisch"),
    ("PAGE", "https://page-online.de/feed/", "Deutsch"),
    ("Yanko Design", "https://www.yankodesign.com/feed/", "Englisch"),
    # Energie und Wirtschaft
    ("Electrek", "https://electrek.co/feed/", "Englisch"),
    ("CleanTechnica", "https://cleantechnica.com/feed/", "Englisch"),
    # Zukunft und Innovation
    ("New Atlas", "https://newatlas.com/index.rss", "Englisch"),
    ("ingenieur.de", "https://www.ingenieur.de/feed/", "Deutsch"),
    ("futurezone", "https://futurezone.at/xml/rss", "Deutsch"),
    ("Singularity Hub", "https://singularityhub.com/feed/", "Englisch"),
    ("MIT Technology Review", "https://www.technologyreview.com/feed/", "Englisch"),
    # Dekoration und Wohnen
    ("Apartment Therapy", "https://www.apartmenttherapy.com/main.rss", "Englisch"),
    ("Homes & Gardens", "https://www.homesandgardens.com/feeds.xml", "Englisch"),
    ("AD Deutschland", "https://www.ad-magazin.de/feed/rss", "Deutsch"),
    ("Livingetc", "https://www.livingetc.com/feeds.xml", "Englisch"),
    # Sport
    ("Sportschau", "https://www.sportschau.de/index~rss2.xml", "Deutsch"),
    ("kicker", "https://newsfeed.kicker.de/news/aktuell", "Deutsch"),
    ("BBC Sport", "https://feeds.bbci.co.uk/sport/rss.xml", "Englisch"),
    ("Guardian Sport", "https://www.theguardian.com/sport/rss", "Englisch"),
    # Bildung
    ("News4teachers", "https://www.news4teachers.de/feed/", "Deutsch"),
    ("Deutsches Schulportal", "https://deutsches-schulportal.de/feed/", "Deutsch"),
    ("Hechinger Report", "https://hechingerreport.org/feed/", "Englisch"),
    # Autos und Mobilität
    ("electrive", "https://www.electrive.net/feed/", "Deutsch"),
    ("ecomento", "https://ecomento.de/feed/", "Deutsch"),
    ("InsideEVs", "https://insideevs.com/rss/news/all/", "Englisch"),
    # Kultur, Film, Serien, Fotografie
    ("Guardian Culture", "https://www.theguardian.com/culture/rss", "Englisch"),
    ("BBC Entertainment", "https://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml", "Englisch"),
    ("Filmstarts", "https://www.filmstarts.de/rss/nachrichten.xml", "Deutsch"),
    ("IndieWire", "https://www.indiewire.com/feed/", "Englisch"),
    ("/Film", "https://www.slashfilm.com/feed/", "Englisch"),
    ("Guardian Film", "https://www.theguardian.com/film/rss", "Englisch"),
    ("Serienjunkies", "https://www.serienjunkies.de/rss/news.xml", "Deutsch"),
    ("DWDL", "https://www.dwdl.de/rss/allethemen.xml", "Deutsch"),
    ("Decider", "https://decider.com/feed/", "Englisch"),
    ("PetaPixel", "https://petapixel.com/feed/", "Englisch"),
    ("My Modern Met", "https://mymodernmet.com/feed/", "Englisch"),
    ("Colossal", "https://www.thisiscolossal.com/feed/", "Englisch"),
    ("Guardian Photography", "https://www.theguardian.com/artanddesign/photography/rss", "Englisch"),
    # Lifestyle
    ("Utopia", "https://utopia.de/feed/", "Deutsch"),
    ("Brigitte", "https://www.brigitte.de/feed.rss", "Deutsch"),
    ("stern Lifestyle", "https://www.stern.de/feed/standard/lifestyle/", "Deutsch"),
    ("Guardian Lifestyle", "https://www.theguardian.com/lifeandstyle/rss", "Englisch"),
    ("Condé Nast Traveler", "https://www.cntraveler.com/feed/rss", "Englisch"),
    # Promis und Kurioses
    ("E! News", "https://www.eonline.com/syndication/feeds/rssfeeds/topstories.xml", "Englisch"),
    ("Hollywood Reporter", "https://www.hollywoodreporter.com/feed/", "Englisch"),
    ("Variety", "https://variety.com/feed/", "Englisch"),
    ("UPI Odd News", "https://rss.upi.com/news/odd_news.rss", "Englisch"),
]
