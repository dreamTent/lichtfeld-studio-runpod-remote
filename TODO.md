
todo:
- [x] add ui basis
    - shows your current pods and jobs
        - non controled pods that this app didnt start have a white circle, controled pods are green (actively recieving status messages), pods that are controled but connection was lost should be yellow, pods that have had an error are blinking red, pods that have unexpectadly disconnected are red
        - if a job doesnt have a runpod yet because the dataset is uploading, if a job is waiting for a pod it should blink blue, it should be blue, if a pod has been created and it hasnt been puppeteered yet it should blink blue,
    - clicking on a job/pod shows you information about the job/pod, its status and current log
    - settings ui for secrets
    - menu for creating builds
    - menu for starting a new job
        - select pod type, select build from ftp server
        - select dataset, if config file is detected use that, if not give option to select a config file
        - set if results should be downloaded automatically, or only put onto the ftp server
    - monitoring
        - create job (the job color will be green blinking)
        - upload dataset to ftp server as tar
        - start pod (if non of that type available wait)
        - upload scripts and start remote controling
- [x] for running lichtfield studio make the remote server act on its own without the clients supervision and shut itself down automatically after uploading the results to ftp, so that if the client gets disconnected the server still runs until the end.
- [ ] implement a build pipeline (later)
- [x] add option to terminate pod (Discard pod in the dashboard)
- [x] add an option to archive or delete a job (the listing not the data)
- [ ] create setup and start script for windows so users just have to execute a bat file
- [ ] create a setup and start script for linux users (debian/ubuntu)
- [ ] make app compatible with windows and linux (debian/ubuntu)


bugs
- after a job is done it switches to completed presumably